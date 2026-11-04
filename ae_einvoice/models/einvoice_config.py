# -*- coding: utf-8 -*-
import json
import logging
from datetime import timedelta
from urllib.parse import urlsplit

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from .cygnet_api_errors import api_error_message, response_json

_logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://staging-uae-api-cygnettaxglobaleinvoicing.cygnet.one"
CYGNET_ENDPOINT_PATHS = {
    "purchase_detail": "/enriched/v0.1/purchase/detail",
    "purchase_lookup": "/enriched/v0.1/purchase/lookup",
    "auth": "/enriched/v0.1/authenticate/token",
    "document_save": "/enriched/v0.1/document/save",
    "status_check": "/enriched/v0.1/reference/status",
}


class EinvoiceConfig(models.Model):
    _name = "einvoice.config"
    _description = "E-Invoice (Cygnet) API Connection Settings"


    base_url = fields.Char(
        string="Base URL",
        required=True,
        default=DEFAULT_BASE_URL,
        groups="base.group_system",
        help="Cygnet server URL only, without the API endpoint path.",
    )
    client_id = fields.Char(string="Client ID", groups="base.group_system")
    client_secret = fields.Char(string="Client Secret", groups="base.group_system")
    username = fields.Char(string="Username", groups="base.group_system")
    password = fields.Char(string="Password", groups="base.group_system")

    access_token = fields.Char(
        string="Access Token", readonly=True, copy=False, groups="base.group_system"
    )
    token_expiry = fields.Datetime(
        string="Token Expiry", readonly=True, copy=False, groups="base.group_system"
    )

    @api.constrains("base_url")
    def _check_base_url(self):
        for config in self:
            try:
                url = urlsplit((config.base_url or "").strip())
                valid = (
                    url.scheme in ("http", "https")
                    and url.hostname
                    and not url.username
                    and not url.password
                    and not url.path.strip("/")
                    and not url.query
                    and not url.fragment
                    and not any(c.isspace() for c in config.base_url.strip())
                )
                url.port  # Validate the optional port as well.
            except ValueError:
                valid = False
            if not valid:
                raise ValidationError(
                    "Enter a valid HTTP or HTTPS server URL without an API path, "
                    "credentials, query parameters, or fragment."
                )

    def write(self, vals):
        if "base_url" in vals:
            vals = dict(vals, access_token=False, token_expiry=False)
        return super().write(vals)

    def _get_endpoint_url(self, endpoint):
        self.ensure_one()
        return self.base_url.strip().rstrip("/") + CYGNET_ENDPOINT_PATHS[endpoint]

    def _request(self, method, endpoint, **kwargs):
        """Retry a refused authentication once, preserving the original request."""
        self.ensure_one()
        if endpoint == "auth":
            raise ValueError("Use _get_access_token for authentication.")
        headers = dict(kwargs.pop("headers", {}))
        headers.setdefault("Content-Type", "application/json")
        kwargs.setdefault("timeout", 30)
        url = self._get_endpoint_url(endpoint)
        send = {"GET": requests.get, "POST": requests.post}[method.upper()]
        headers["auth-token"] = self._get_access_token()
        response = send(url, headers=dict(headers), **kwargs)
        if response.status_code == 401:
            headers["auth-token"] = self._get_access_token(force_refresh=True)
            response = send(url, headers=dict(headers), **kwargs)
            if response.status_code == 401:
                raise UserError(api_error_message(
                    "Cygnet authentication failed after refreshing the token (%s)" % endpoint,
                    response=response))
        return response

    def _request_logged(self, call_type, method, endpoint, log_vals=None, **kwargs):
        """Like _request, but records every call (and failure) in einvoice.log.

        The token request made by _get_access_token is not logged.
        """
        self.ensure_one()
        log_model = self.env["einvoice.log"]
        vals = dict(log_vals or {}, direction="ap", call_type=call_type)
        try:
            vals["request_payload"] = json.dumps(
                kwargs.get("json"), indent=2, ensure_ascii=False, default=str)
        except Exception:
            vals["request_payload"] = str(kwargs.get("json"))
        try:
            response = self._request(method, endpoint, **kwargs)
        except Exception as error:
            log_model._write_log(dict(
                vals, status_code="ERROR", success=False,
                response_payload=str(error),
            ))
            raise
        try:
            try:
                body = json.dumps(response.json(), indent=2, ensure_ascii=False, default=str)
                parsed = True
            except ValueError:
                body, parsed = response.text, False
            vals.update(
                status_code=str(response.status_code),
                response_payload=body if isinstance(body, str) else str(body),
                success=bool(response.ok and parsed),
            )
        except Exception:
            _logger.exception("Could not read the %s response for logging", endpoint)
            vals.update(status_code="UNKNOWN", success=False)
        log_model._write_log(vals)
        return response

    def action_open_einvoice_config(self):

        config = self.sudo().search([], limit=1)
        if not config:
            config = self.sudo().create({})
        return {
            "type": "ir.actions.act_window",
            "name": "E-Invoice Configuration",
            "res_model": "einvoice.config",
            "view_mode": "form",
            "res_id": config.id,
            "target": "current",
        }

    def _get_access_token(self, force_refresh=False):

        self.ensure_one()
        if (
            not force_refresh
            and self.access_token
            and self.token_expiry
            and self.token_expiry > fields.Datetime.now()
        ):
            return self.access_token

        try:
            response = requests.post(
                self._get_endpoint_url("auth"),
                headers={
                    "client-id": self.client_id,
                    "client-secret": self.client_secret,
                    "Content-Type": "application/json",
                },
                json={
                    "forceRefresh": force_refresh,
                    "username": self.username,
                    "password": self.password,
                },
                timeout=15,
            )
        except requests.RequestException as error:
            raise UserError(api_error_message("Cygnet authentication failed", error=error)) from error
        data = response_json(response, "Cygnet authentication failed")

        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise UserError(api_error_message("Cygnet authentication returned no token", response=response))
        expiry_minutes = data.get("expiryTimeInMinutes", 60)
        self.sudo().write(
            {
                "access_token": token,
                "token_expiry": fields.Datetime.now() + timedelta(minutes=expiry_minutes),
            }
        )
        return token

    def action_generate_token(self):

        self.ensure_one()
        self.sudo()._get_access_token(force_refresh=True)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "E-Invoice Connection",
                "message": "Token generated successfully.",
                "type": "success",
                "sticky": False,
            },
        }
