# -*- coding: utf-8 -*-
import json
from datetime import timedelta

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from .cygnet_api_errors import api_error_message, response_json


class ResCompany(models.Model):
    _inherit = "res.company"

    einvoice_ap_fetched_up_to = fields.Datetime(
        string="AP Invoices Fetched Up To",
        readonly=True,
        help="The 'To Date' used in the last AP fetch from Cygnet for this "
             "company. Shows how far the data is up to date, not when the "
             "fetch was clicked. Overwritten on every fetch.",
    )
class EinvoicePurchase(models.Model):
    _name = "einvoice.purchase"
    _description = "Cygnet AP Invoice Lookup"
    _rec_name = "document_number"
    _order = "document_date desc, id desc"
    _check_company_auto = True

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company, index=True)
    country = fields.Char(readonly=True)
    entity_tax_reg_no = fields.Char(string="Entity TRN", required=True, readonly=True)
    entity_tin = fields.Char(string="Entity TIN", readonly=True)
    entity_name = fields.Char(readonly=True)
    supply_type = fields.Char(readonly=True)
    document_type = fields.Char(required=True, readonly=True)
    document_number = fields.Char(required=True, readonly=True, index=True)
    document_date = fields.Date(readonly=True)
    supplier_tax_reg_no = fields.Char(string="Supplier TRN", required=True, readonly=True, index=True)
    lookup_payload = fields.Text(string="Lookup Response", readonly=True)
    date_added = fields.Datetime(default=fields.Datetime.now, readonly=True)
    last_fetched = fields.Datetime(readonly=True)

    _document_unique = models.Constraint(
        "UNIQUE(company_id, entity_tax_reg_no, supplier_tax_reg_no, document_type, document_number)",
        "This supplier document has already been fetched for this company.",
    )


class EinvoicePurchaseFetch(models.TransientModel):
    _name = "einvoice.purchase.fetch"
    _description = "Fetch Cygnet AP Invoices"

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    entity_tax_reg_no = fields.Char(string="Entity TRN", required=True, default=lambda self: self.env.company.vat)
    entity_tin = fields.Char(string="Entity TIN", required=True,
                             default=lambda self: "".join(c for c in (self.env.company.vat or "") if c.isdigit())[:10])
    entity_name = fields.Char(required=True, default=lambda self: self.env.company.name)
    country = fields.Char(required=True, default="AE")
    from_document_date = fields.Datetime(required=True, default=lambda self: fields.Datetime.now() - timedelta(days=7))
    to_document_date = fields.Datetime(required=True, default=fields.Datetime.now)
    from_stamp = fields.Datetime(string="From Timestamp", required=True,
                                 default=lambda self: fields.Datetime.now() - timedelta(days=7))
    to_stamp = fields.Datetime(string="To Timestamp", required=True, default=fields.Datetime.now)
    last_fetched_up_to = fields.Datetime(
        related="company_id.einvoice_ap_fetched_up_to", readonly=True)

    @api.onchange("company_id")
    def _onchange_company_id(self):
        self.entity_tax_reg_no = self.company_id.vat
        self.entity_tin = "".join(c for c in (self.company_id.vat or "") if c.isdigit())[:10]
        self.entity_name = self.company_id.name
        self.country = self.company_id.country_id.code or "AE"

    @api.constrains("from_document_date", "to_document_date", "from_stamp", "to_stamp")
    def _check_dates(self):
        for wizard in self:
            if wizard.from_document_date > wizard.to_document_date:
                raise ValidationError("The document start date must be before the end date.")
            if bool(wizard.from_stamp) != bool(wizard.to_stamp):
                raise ValidationError("Provide both timestamp limits or leave both empty.")
            if wizard.from_stamp and wizard.from_stamp > wizard.to_stamp:
                raise ValidationError("The start timestamp must be before the end timestamp.")

    def action_fetch(self):
        self.ensure_one()
        if self.company_id not in self.env.companies:
            raise UserError("Select a company you are currently allowed to access.")
        config = self.env["einvoice.config"].sudo().search([], limit=1)
        if not config:
            raise UserError("Configure the Cygnet connection under E-Invoice > Configuration first.")
        payload = {
            "start": 0, "size": 20,
            "entities": [{"country": self.country, "entityTaxRegNo": self.entity_tax_reg_no,
                          "entityTin": self.entity_tin, "entityName": self.entity_name}],
            "fromDocumentDate": fields.Datetime.to_string(self.from_document_date),
            "toDocumentDate": fields.Datetime.to_string(self.to_document_date),
            "country": self.country,
            "downloadAdditionalAttachment": False,
        }
        if self.from_stamp:
            payload.update(fromStamp=fields.Datetime.to_string(self.from_stamp),
                           toStamp=fields.Datetime.to_string(self.to_stamp))
        purchases = self.env["einvoice.purchase"]
        fetched_ids = set()
        seen_pages = set()
        # Bound a synchronous fetch to protect against inconsistent pagination.
        for page in range(500):
            try:
                response = config._request_logged(
                    "ap_lookup", "POST", "purchase_lookup",
                    log_vals={"company_id": self.company_id.id,
                              "reference": "%s (offset %s)" % (self.entity_tax_reg_no, payload["start"])},
                    json=payload, timeout=30,
                )
            except requests.RequestException as error:
                raise UserError(api_error_message("Cygnet purchase lookup failed", error=error)) from error
            data = response_json(response, "Cygnet purchase lookup failed (offset %s)" % payload["start"])
            if not isinstance(data, dict) or not isinstance(data.get("result"), list):
                raise UserError(api_error_message("Invalid Cygnet purchase lookup response", response=response))
            rows = data["result"]
            total = data.get("totalRecords")
            if type(total) is not int or total < 0:
                raise UserError("Cygnet returned an invalid totalRecords value.")
            if not rows:
                if payload["start"] < total:
                    raise UserError("Cygnet returned an empty page before all invoices were fetched. Please retry.")
                break
            signature = json.dumps(rows, sort_keys=True)
            if signature in seen_pages:
                raise UserError("Cygnet repeated a lookup page. Fetch cancelled to avoid incomplete results.")
            seen_pages.add(signature)
            for row in rows:
                if not isinstance(row, dict) or any(
                    not isinstance(row.get(key), str) or not row[key].strip()
                    for key in ("entityTaxRegNo", "billFromTaxRegNo", "documentType", "documentNumber", "documentDate")
                ):
                    raise UserError("A Cygnet purchase record is missing required document identifiers or date.")
                if row["entityTaxRegNo"] != self.entity_tax_reg_no:
                    raise UserError("Cygnet returned a document for a different entity TRN.")
                try:
                    document_date = fields.Date.to_date(row["documentDate"][:10])
                except (ValueError, TypeError):
                    raise UserError("Cygnet returned an invalid document date.")
                identity = {
                    "company_id": self.company_id.id,
                    "entity_tax_reg_no": row["entityTaxRegNo"],
                    "supplier_tax_reg_no": row["billFromTaxRegNo"],
                    "document_type": row["documentType"],
                    "document_number": row["documentNumber"],
                }
                vals = dict(identity, country=row.get("country"), entity_tin=row.get("entityTin"),
                            entity_name=row.get("entityName"), supply_type=row.get("supplyType"),
                            document_date=document_date, lookup_payload=json.dumps(row, indent=2),
                            last_fetched=fields.Datetime.now())
                purchase = purchases.search([(key, "=", value) for key, value in identity.items()], limit=1)
                if purchase:
                    if purchase.state != "rejected":
                        purchase.write(vals)
                else:
                    purchase = purchases.create(vals)
                fetched_ids.add(purchase.id)
            payload["start"] += len(rows)
            if payload["start"] >= total:
                break
        else:
            raise UserError("Too many purchase lookup pages. Select a smaller date range.")
        self.company_id.sudo().write({"einvoice_ap_fetched_up_to": self.to_document_date})
        action = self.env["ir.actions.actions"]._for_xml_id("ae_einvoice.action_einvoice_ap")
        action["domain"] = [("id", "in", sorted(fetched_ids))]
        return action

