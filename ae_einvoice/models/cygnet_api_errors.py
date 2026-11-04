"""Readable Cygnet errors for Odoo popups, without request credentials."""
import json

import requests

from odoo.exceptions import UserError


def api_error_message(operation, response=None, error=None):
    heading = operation
    if response is None and error is not None:
        response = getattr(error, "response", None)
    if response is not None:
        heading += " (HTTP %s)" % response.status_code
        try:
            data = response.json()
        except ValueError:
            detail = response.text.strip()
        else:
            # Prefer provider error fields over unrelated document/response data.
            if isinstance(data, dict):
                details = {key: value for key, value in data.items()
                           if key.lower() in {"message", "error", "errors", "errormessage", "errordetails",
                                              "detail", "details", "title", "code", "errorcode", "description"}
                           and value is not None and value != ""}
                data = details or data

            def redact(value):
                if isinstance(value, dict):
                    return {key: ("[redacted]" if any(word in key.lower().replace("-", "").replace("_", "")
                                 for word in ("token", "password", "secret", "authorization")) else redact(child))
                            for key, child in value.items()}
                if isinstance(value, list):
                    return [redact(child) for child in value]
                return value

            detail = json.dumps(redact(data), indent=2, ensure_ascii=False)
        detail = detail or "Cygnet returned an empty response body."
    elif isinstance(error, requests.Timeout):
        detail = "The request timed out before Cygnet returned a response. Please retry."
    elif isinstance(error, requests.ConnectionError):
        detail = "Could not connect to Cygnet. Check the configured base URL and network connection."
    else:
        detail = "The request could not be completed; no response was received from Cygnet."
    if len(detail) > 4000:
        detail = detail[:4000] + "\n[Response truncated]"
    return "%s\n\n%s" % (heading, detail)


def response_json(response, operation):
    try:
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as error:
        raise UserError(api_error_message(operation, response=response, error=error)) from error
