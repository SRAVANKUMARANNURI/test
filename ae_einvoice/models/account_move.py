# -*- coding: utf-8 -*-
import json
import logging
import requests
from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

def _map_einvoice_status(push_status):
    """Map eInv.pushStatus to the einvoice_status states
    (draft/submitted/accepted/rejected)."""
    if not push_status:
        return None
    s = push_status.strip().lower()
    if s in ("generated", "accepted", "success", "approved"):
        return "generated"
    if s in ("rejected", "failed", "declined", "error"):
        return "rejected"
    return None  # still pending/unrecognized — leave as Submitted


def _map_peppol_status(push_status):
    """Map peppol/peppolMls pushStatus to the peppol status states
    (draft/pending/rejected/accepted)."""
    if not push_status:
        return "pending"
    s = push_status.strip().lower()
    if s in ("delivered", "accepted", "success", "approved"):
        return "accepted"
    if s in ("rejected", "failed", "declined", "error"):
        return "rejected"
    return "pending"
def _map_taxAuthorityMls_status(push_status):
    """Map peppol/peppolMls pushStatus to the peppol status states
    (draft/pending/rejected/accepted)."""
    if not push_status:
        return "pending"
    s = push_status.strip().lower()
    if s in ("delivered", "accepted", "success", "approved"):
        return "accepted"
    if s in ("rejected", "failed", "declined", "error"):
        return "rejected"
    return "pending"

# Odoo UoM display name (lowercased) -> UN/ECE Rec 20 code. Extend as needed;
# anything not listed falls back to the invoice's einv_unit_code_id default.
_UOM_NAME_TO_EINV_CODE = {
    "day": "DAY", "days": "DAY",
    "month": "MON", "months": "MON",
    "hour": "HUR", "hours": "HUR",
    "year": "ANN", "years": "ANN",
    "week": "WEE", "weeks": "WEE",
    "unit": "XUN", "units": "XUN",

}




# Odoo res.country.state code (UAE) -> Cygnet subdivision code.
_UAE_STATE_CODE_TO_EINV_CODE = {
    "AZ": "AUH",   # Abu Dhabi
    "AA": "AUH",   # Al Ain (part of Abu Dhabi emirate)
    "DU": "DXB",   # Dubai
    "SH": "SHJ",   # Sharjah
    "AJ": "AJM",   # Ajman
    "UQ": "UAQ",   # Umm al-Quwain
    "RK": "RAK",   # Ras al-Khaimah
    "FU": "FUJ",   # Fujairah
}


def _einv_subdivision_code(state):
    return _UAE_STATE_CODE_TO_EINV_CODE.get(state.code, "") if state else ""

class AccountMove(models.Model):
    _inherit = "account.move"


    einvoice_selected = fields.Boolean(
        string="Sent to E-Invoice",
        default=False,
        copy=False,
        help="Set when the invoice is explicitly sent from Customer Invoices "
             "to the E-Invoice > AR menu. Only invoices with this flag show "
             "up in the AR menu.",
    )
    einvoice_date_added = fields.Datetime(
        string="Date Added",
        readonly=True,
        copy=False,
        help="Date and time the invoice was last added to the E-Invoice workspace. "
             "Sending an invoice already in the workspace keeps its existing date.",
    )
    einvoice_status = fields.Selection(
        [
            ("draft", "Draft"),
            ("submitted", "Submitted (Pending)"),
            ("generated", "Generated"),
            ("rejected", "Rejected"),
        ],
        string="E-Invoice Status",
        default="draft",
        copy=False,
        tracking=True,
    )
    einvoice_peppol_status= fields.Selection(
        [
            ("draft", "Draft"),
            ("pending","Pending"),
            ("rejected", "Rejected"),
            ("accepted", "Accepted"),

        ],
        string="Peppol Status",
        default="draft",
        copy=False,
        tracking=True,
    )
    einvoice_peppol_mls_status= fields.Selection(
        [
            ("draft", "Draft"),
            ("pending","Pending"),
            ("rejected", "Rejected"),
            ("accepted", "Accepted"),

        ],
        string="Peppol mls Status",
        default="draft",
        copy=False,
        tracking=True,
    )
    einv_document_uuid = fields.Char(
        string="Document UUID",
        copy=False,
        readonly=True,
    )

    einvoice_tax_authority_mls_status = fields.Selection(
        [
            ("draft", "Draft"),
            ("pending", "Pending"),
            ("rejected", "Rejected"),
            ("accepted", "Accepted"),

        ],
        string="Tax Authority MLS (FTA C5)",
        default="draft",
        copy=False,
        tracking=True,
    )
    einvoice_reference = fields.Char(
        string="E-Invoice Reference",
        copy=False,
        readonly=True,
        help="Reference string returned by the authority/API upon submission.",
    )
    einvoice_json_payload = fields.Text(
        string="E-Invoice JSON Payload",
        copy=False,
        readonly=True,
        help="Outgoing JSON payload built from this invoice (PINT AE format).",
    )
    einv_attachment_ids = fields.One2many(
        "einvoice.attachment", "move_id", string="E-Invoice Attachments", copy=False)
    einvoice_submit_response = fields.Text(
        string="Submit API Response",
        copy=False,
        readonly=True,
    )
    einvoice_status_response = fields.Text(
        string="Status Check API Response",
        copy=False,
        readonly=True,
    )
    einvoice_log_ids = fields.One2many(
        "einvoice.log",
        "invoice_id",
        string="E-Invoice API Logs",
    )
    einvoice_log_count = fields.Integer(
        string="E-Invoice Log Count",
        compute="_compute_einvoice_log_count",
    )

    einv_transaction_type_code = fields.Selection(
        [
            ("10000000", "Free trade zone"),
            ("01000000", "Deemed supply"),
            ("00100000", "Profit Margin Scheme"),
            ("00010000", "Summary invoice"),
            ("00001000", "Continuous Supply"),
            ("00000100", "Agent billing"),
            ("00000010", "Supply through E-commerce"),
            ("00000001", "Exports"),
            ("00000000", "B2B Transaction"),
        ],
        string="Transaction Type Code",
        default="00000000",
        copy=False,
    )

    einv_document_type = fields.Selection(
        [
            ("INV", "Invoice"),
            ("CR", "Credit Note"),
            ("INVRESP", "Invoice Response"),
            ("SBINV", "Self-billed Invoice"),
            ("SBCR", "Self-billed Credit Note"),
        ],
        string="Document Type",
        compute="_compute_einv_document_type",
        store=True,
        readonly=False,
        copy=False,
    )

    einv_document_type_purpose = fields.Selection(
        [
            ("380", "Commercial invoice"),
            ("480", "Invoice out of scope of tax"),
            ("81", "Credit note related to goods or services"),
            ("381", "Credit note"),
            ("389", "Self-billed invoice"),
            ("261", "Self billed credit note"),
        ],
        string="Document Type Purpose",
        compute="_compute_einv_document_type_purpose",
        store=True,
        readonly=False,
        copy=False,
    )

    einv_agency_id = fields.Selection(
        [
            ("TL", "Trade license"),
            ("EID", "Emirates ID"),
            ("CD", "Cabinet decision"),
            ("PAS", "Passport"),
        ],
        string="Bill From Legal Agency ID",
        default="TL",
        copy=False,
    )

    einv_payment_mode = fields.Selection(
        [
            ("1", "Instrument not defined"),
            ("10", "In cash"),
            ("20", "Cheque"),
            ("21", "Banker's draft"),
            ("30", "Credit transfer"),
            ("49", "Direct debit"),
            ("54", "Credit card"),
            ("55", "Debit card"),
            ("68", "Online payment service"),
        ],
        string="Payment Mode",
        default="30",
        copy=False,
    )

    einv_tax_category = fields.Selection(
        [
            ("S", "Standard rate"),
            ("E", "Exempt from tax"),
            ("O", "Services outside scope of tax / Not subject to tax"),
            ("AE", "VAT Reverse Charge"),
            ("Z", "Zero rated"),
            ("N", "Standard rate additional VAT"),
        ],
        string="Tax Category Code",
        default="S",
        copy=False,
    )



    einv_unit_code_id = fields.Many2one(
        "einvoice.unit.code",
        string="Invoiced Quantity Unit Code",
        default= lambda self: self.env["einvoice.unit.code"].search([("code", "=", "C62")], limit=1),
        copy=False,
    )

    einv_allowance_reason = fields.Selection(
        [
            ("41", "Bonus for works ahead of schedule"),
            ("42", "Other bonus"),
            ("60", "Manufacturer's consumer discount"),
            ("62", "Due to military status"),
            ("63", "Due to work accident"),
            ("64", "Special agreement"),
            ("65", "Production error discount"),
            ("66", "New outlet discount"),
            ("67", "Sample discount"),
            ("68", "End-of-range discount"),
            ("70", "Incoterm discount"),
            ("71", "Point of sales threshold allowance"),
            ("88", "Material surcharge/deduction"),
            ("95", "Discount"),
            ("100", "Special rebate"),
            ("102", "Fixed long term"),
            ("103", "Temporary"),
            ("104", "Standard"),
            ("105", "Yearly turnover"),
        ],
        string="Allowance Reason",
        default="95",
        copy=False,
    )

    @api.depends("move_type")
    def _compute_einv_document_type_purpose(self):
        for move in self:
            move.einv_document_type_purpose = "381" if move.move_type == "out_refund" else "380"

    @api.depends("move_type")
    def _compute_einv_document_type(self):
        for move in self:
            move.einv_document_type = "CR" if move.move_type == "out_refund" else "INV"

    @api.depends("einvoice_log_ids")
    def _compute_einvoice_log_count(self):
        for move in self:
            move.einvoice_log_count = len(move.einvoice_log_ids)

    # # ------------------------------------------------------------------
    # # Lock invoice once Accepted
    # # ------------------------------------------------------------------
    # def write(self, vals):
    #     # Allow the e-invoice fields themselves (and technical fields) to be
    #     # written by our own buttons even when the invoice is Accepted.
    #     einvoice_technical_fields = {
    #         "einvoice_status",
    #         "einvoice_reference",
    #         "einvoice_json_payload",
    #         "einvoice_submit_response",
    #         "einvoice_status_response",
    #         "einvoice_peppol_status",
    #         "einvoice_peppol_mls_status",
    #         "einvoice_tax_authority_mls_status",
    #     }
    #     if set(vals.keys()) - einvoice_technical_fields:
    #         for move in self:
    #             if move.einvoice_status == "generated":
    #                 raise UserError(
    #                     "This invoice has been Generated for e-invoicing and "
    #                     "can no longer be edited."
    #                 )
    #     return super().write(vals)
    #



    def _einv_unit_code_for_line(self, line):
        """Resolve the E-Invoice unit code for a line: try to match the
        Odoo UoM name against known synonyms, otherwise fall back to the
        invoice's own Invoiced Quantity Unit Code default."""
        self.ensure_one()
        uom_name = (line.product_uom_id.name or "").strip().lower()
        code = _UOM_NAME_TO_EINV_CODE.get(uom_name)
        if code:
            return code
        return self.einv_unit_code_id.code

    def _build_einvoice_payload(self):
        self.ensure_one()
        move = self
        company = move.company_id
        partner = move.partner_id

        # Rate to convert invoice-currency amounts into the company (accounting)
        # currency. 1.0 when the invoice is already in company currency.
        acc_rate = 1.0
        if move.currency_id != company.currency_id and move.invoice_currency_rate:
            acc_rate = round(1.0 / move.invoice_currency_rate, 6)

        def acc(amount):
            return round(amount * acc_rate, 2)

        def uae_tin(vat):
            """First 10 digits of a 15-digit TRN, per the entityTIN pattern
            observed in the sample payload."""
            digits = "".join(ch for ch in (vat or "") if ch.isdigit())
            return digits[:10]

        def uae_endpoint(vat):
            # TODO: confirm "0235" is the correct fixed scheme prefix for AE
            tin = uae_tin(vat)
            return "0235:%s" % tin if tin else ""

        def _r2(value):
            """Round to 2 decimals """
            return round(value, 2) if isinstance(value, (int, float)) else value

        def _round_payload_numbers(obj):
            if isinstance(obj, dict):
                return {k: _round_payload_numbers(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_round_payload_numbers(v) for v in obj]
            if isinstance(obj, float):
                return round(obj, 2)
            return obj

        entity_tin = uae_tin(company.vat)
        bill_from_endpoint = uae_endpoint(company.vat)
        bill_to_endpoint = uae_endpoint(partner.vat)

        vat_totals = {}
        items = []
        for i, line in enumerate(move.invoice_line_ids.filtered(lambda l: l.display_type== 'product'), start=1):
            tax_rate = 0.0
            tax_category = move.einv_tax_category or "S"
            if line.tax_ids:
                tax_rate = line.tax_ids[0].amount
            taxable_amount = line.price_subtotal
            tax_amount = line.price_total - line.price_subtotal
            net_unit_price = (taxable_amount / line.quantity) if line.quantity else line.price_unit
            unit_allowance_amount = line.price_unit - net_unit_price
            allowance_details = []
            if line.discount:
                allowance_details.append(
                    {
                        "allowanceReason": move.einv_allowance_reason or "95",
                        "allowanceBaseAmount": line.price_unit * line.quantity,
                        "allowancePercentage": line.discount,
                        "allowanceAmount": unit_allowance_amount * line.quantity,
                    }
                )
            items.append(
                {
                    "serialNumber": str(i),
                    "name": line.product_id.display_name or line.name,
                    "description": line.name,
                    "isService": "Y" if line.product_id.type == "service" else "N",
                    "taxCategoryCode": tax_category,
                    "taxRate": tax_rate,
                    "invoicedQuantity": line.quantity,

                    "invoicedQuantityUnitCode": move._einv_unit_code_for_line(line),
                    "unitPrice": line.price_unit,
                    "unitAllowanceAmount": 0,
                    "netUnitPrice": line.price_unit,
                    "priceBaseQuantity": 1.0,
                    "allowanceDetails": allowance_details,
                    "taxableAmount": taxable_amount,
                    "taxAmount": tax_amount,

                    "taxAmountInAccCurrency": acc(tax_amount),
                    "lineAmountInAccCurrency": acc(line.price_total),
                }
            )
            key = (tax_category, tax_rate)
            bucket = vat_totals.setdefault(
                key, {"vatCategoryTaxableAmount": 0.0, "vatCategoryTaxAmount": 0.0}
            )
            # bucket["vatCategoryTaxableAmount"] += taxable_amount
            # bucket["vatCategoryTaxAmount"] += tax_amount
            bucket["vatCategoryTaxableAmount"] = _r2(bucket["vatCategoryTaxableAmount"] + taxable_amount)
            bucket["vatCategoryTaxAmount"] = _r2(bucket["vatCategoryTaxAmount"] + tax_amount)

        vat_category_wise_total = [
            {
                "vatCategoryTaxableAmount": _r2(v["vatCategoryTaxableAmount"]),
                "vatCategoryTaxAmount": _r2(v["vatCategoryTaxAmount"]),
                "vatCategoryCode": k[0],
                "vatCategoryRate": _r2(k[1]),
            }
            for k, v in vat_totals.items()
        ]

        payload = {
            "data": [
                {
                    "country": "AE",
                    "entityTaxRegNo": company.vat or "",
                    "entityTIN": entity_tin,
                    "entityName": company.name,

                    "purpose": "PEDEL",
                    "autoPushOrGenerate": "PEDEL",
                    "supplyType": "S",
                    "transactionType": "NOR",
                    "transactionTypeCode": move.einv_transaction_type_code or "00000000",
                    "documentType": move.einv_document_type or ("INV" if move.move_type == "out_invoice" else "CR"),
                    "documentTypePurpose": move.einv_document_type_purpose or ("380" if move.move_type == "out_invoice" else "381"),
                    "documentNumber": move.name,
                    "purchaseOrderNumber":move.ref or "",
                    "salesOrderNumber":move.invoice_origin or "",
                    "documentDate": str(move.invoice_date or ""),
                    "documentCurrency": move.currency_id.name,
                    "billFromTaxRegNo": company.vat or "",
                    "billFromLegalRegNo": "0235-%s" % (company.vat or ""),
                    "billFromLegalName": company.name,
                    "billFromEndpoint": bill_from_endpoint,
                    "billFromStreetName": company.street or "",
                    "billFromCity": company.city or "",
                    "billFromCountrySubdivision":_einv_subdivision_code(company.state_id),
                    "billFromCountry": company.country_id.code or "AE",
                    "billToLegalName": partner.name or "",
                    "billToEndpoint": bill_to_endpoint,
                    "billToStreetName": partner.street or "",
                    "billToCity": partner.city or "",
                    "billToCountrySubdivision":_einv_subdivision_code(partner.state_id),
                    "billToCountry": partner.country_id.code or "",
                    "paymentDueDate": str(move.invoice_date_due or ""),

                    "billFromLegalAgency": [
                        {"AgencyID": move.einv_agency_id or "TL", "AgencyName": company.name},

                    ],
                    "totalTaxAmountInAccCurrency": acc(move.amount_tax),
                    "totalDocAmountInAccCurrency":acc(move.amount_total),
                    "vatCategoryWiseTotal": vat_category_wise_total,
                    "TotalLineNetAmount": move.amount_untaxed,
                    "invoiceTotalAmountWithoutTax": move.amount_untaxed,
                    "invoiceTotalTaxAmount": move.amount_tax,
                    "totalDocumentAmount": move.amount_total,
                    "paymentAmountDue": move.amount_residual,

                    "paymentDetails": [
                        {
                            "paymentMode": move.einv_payment_mode or "30",
                            "creditPaymentAccNumber": (
                                company.partner_id.bank_ids[:1].acc_number or ""
                                if company.partner_id.bank_ids
                                else ""
                            ),
                        }
                    ],
                    "items": items,
                }
            ]
        }
        payload = _round_payload_numbers(payload)
        if move.einv_attachment_ids:
            payload["data"][0]["additionalDocumentRefDetails"] = [
                att._einv_payload_dict() for att in move.einv_attachment_ids
            ]
        if move.currency_id != company.currency_id:
            payload["data"][0]["exchangeRate"] = acc_rate
        return payload

    def action_einvoice_view_json(self):

        for move in self:
            payload = move._build_einvoice_payload()
            move.einvoice_json_payload = json.dumps(payload, indent=2, default=str)
        return True

    def action_einvoice_submit(self):
        errors = []
        for move in self:
            if move.einvoice_status == "generated":
                raise UserError("This invoice has already been Generated.")

            if not move.einvoice_json_payload or move.einvoice_status == "rejected":
                move.action_einvoice_view_json()

            try:
                response = move._einvoice_call_submit_api(move.einvoice_json_payload)
            except Exception as e:
                response = {"status_code": "ERROR", "reference": None, "raw": {"error": str(e)}}

            reference = response.get("reference")

            move.write(
                {
                    "einvoice_reference": reference,
                    "einvoice_submit_response": json.dumps(response, indent=2, default=str),
                    "einvoice_status": "submitted" if reference else move.einvoice_status,
                }
            )
            self.env["einvoice.log"].create(
                {
                    "invoice_id": move.id,
                    "call_type": "submit",
                    "request_payload": move.einvoice_json_payload,
                    "response_payload": json.dumps(response, indent=2, default=str),
                    "status_code": str(response.get("status_code", "")),
                    "success": bool(reference),
                }
            )
            # Commit now so this log/write survives even if we raise below —
            # otherwise Odoo rolls back the whole transaction and the audit
            # trail disappears along with it.
            self.env.cr.commit()

            if not reference:
                errors.append(
                    "%s: %s" % (move.name, json.dumps(response.get("raw", response), default=str))
                )

        if errors:
            raise UserError("E-Invoice submission failed for:\n\n" + "\n\n".join(errors))
        return True

    def action_einvoice_submit_bulk(self):
        """Header button on the AR E-Invoice list view — submits every
        selected invoice that is eligible (Draft or Rejected), skipping
        anything already Submitted/Generated, without needing to change
        action_einvoice_submit itself."""
        moves_to_submit = self.filtered(lambda m: m.einvoice_status in ("draft", "rejected"))
        skipped = self - moves_to_submit

        if not moves_to_submit:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "E-Invoice Submit",
                    "message": "None of the selected invoices are eligible for submission.",
                    "type": "warning",
                    "sticky": False,
                },
            }

        messages = []
        notif_type = "success"

        try:
            moves_to_submit.action_einvoice_submit()
        except UserError as e:
            messages.append(str(e))
            notif_type = "danger"

        if skipped:
            messages.append(
                "Skipped (not Draft/Rejected): %s" % ", ".join(skipped.mapped("name"))
            )
            if notif_type == "success":
                notif_type = "warning"

        if not messages:
            messages.append("All selected invoices submitted successfully.")

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "E-Invoice Submit",
                "message": "\n".join(messages),
                "type": notif_type,
                "sticky": notif_type != "success",
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def action_einvoice_check_status(self):
        errors = []
        for move in self:
            if move.einvoice_status not in ("submitted", "generated"):
                raise UserError(
                    "Status can only be checked for invoices in 'Submitted (Pending)' state."
                )

            try:
                response = move._einvoice_call_status_api(move.einvoice_reference)
            except Exception as e:
                response = {
                    "status_code": "ERROR",
                    "status": None,
                    "peppol_status": None,
                    "peppol_mls_status": None,
                    "raw": {"error": str(e)},
                }

            new_status = response.get("status")
            document_uuid = response.get("document_uuid")
            peppol_status = response.get("peppol_status")
            peppol_mls_status = response.get("peppol_mls_status")
            tax_authority_mls_status = response.get("tax_authority_mls_status")

            move.write(
                {
                    "einvoice_status_response": json.dumps(response, indent=2, default=str),
                    "einvoice_status": new_status if new_status in ("generated", "rejected") else move.einvoice_status,
                    "einv_document_uuid": document_uuid or move.einv_document_uuid,
                    "einvoice_peppol_status": peppol_status or move.einvoice_peppol_status,
                    "einvoice_peppol_mls_status": peppol_mls_status or move.einvoice_peppol_mls_status,
                    "einvoice_tax_authority_mls_status": tax_authority_mls_status or move.einvoice_tax_authority_mls_status,
                }
            )
            self.env["einvoice.log"].create(
                {
                    "invoice_id": move.id,
                    "call_type": "status_check",
                    "request_payload": json.dumps({"reference": move.einvoice_reference}),
                    "response_payload": json.dumps(response, indent=2, default=str),
                    "status_code": str(response.get("status_code", "")),
                    "success": new_status in ("generated", "rejected"),
                }
            )
            self.env.cr.commit()

            if response.get("status_code") == "ERROR":
                errors.append("%s: %s" % (move.name, response["raw"]["error"]))

        if errors:
            raise UserError("E-Invoice status check failed for:\n\n" + "\n\n".join(errors))
        return True

    def action_einvoice_bulk_status_check(self):

        def needs_check(m):
            if m.einvoice_status not in ("submitted", "generated"):
                return False
            fully_accepted = (
                m.einvoice_status == "generated"
                and m.einvoice_peppol_status == "accepted"
                and m.einvoice_peppol_mls_status == "accepted"
                and m.einvoice_tax_authority_mls_status == "accepted"
            )
            return not fully_accepted

        moves_to_check = self.filtered(needs_check)
        skipped = self - moves_to_check

        if not moves_to_check:
            raise UserError(
                "None of the selected invoices need a status check "
                "(either not yet submitted, or already fully Accepted)."
            )

        result = moves_to_check.action_einvoice_check_status()

        if skipped:
            raise UserError(
                "Checked status for the eligible invoice(s). Skipped (not "
                "submitted, or already fully Accepted): %s"
                % ", ".join(skipped.mapped("name"))
            )
        return result

    # def action_einvoice_submit(self):
    #
    #
    #     for move in self:
    #         if move.einvoice_status == "generated":
    #             raise UserError("This invoice has already been Generated.")
    #
    #         if not move.einvoice_json_payload:
    #             move.action_einvoice_view_json()
    #
    #         response = move._einvoice_call_submit_api(move.einvoice_json_payload)
    #         reference = response.get("reference")
    #
    #         move.write(
    #             {
    #                 "einvoice_reference": response.get("reference"),
    #                 "einvoice_submit_response": json.dumps(response, indent=2, default=str),
    #                 "einvoice_status": "submitted" if reference else move.einvoice_status,
    #             }
    #         )
    #         self.env["einvoice.log"].create(
    #             {
    #                 "invoice_id": move.id,
    #                 "call_type": "submit",
    #                 "request_payload": move.einvoice_json_payload,
    #                 "response_payload": json.dumps(response, indent=2, default=str),
    #                 "status_code": str(response.get("status_code", "")),
    #                 "success": bool(response.get("reference")),
    #             }
    #         )
    #         if not reference:
    #             raise UserError(
    #                 "E-Invoice submission failed — no reference was returned.\n\n%s"
    #                 % json.dumps(response.get("raw", response), indent=2, default=str)
    #             )
    #     return True
    #
    # def action_einvoice_check_status(self):
    #
    #     for move in self:
    #         if move.einvoice_status != "submitted":
    #             raise UserError(
    #                 "Status can only be checked for invoices in 'Submitted (Pending)' state."
    #             )
    #
    #         response = move._einvoice_call_status_api(move.einvoice_reference)
    #         new_status = response.get("status")
    #         peppol_status = response.get("peppol_status")
    #         peppol_mls_status = response.get("peppol_mls_status")
    #
    #         move.write(
    #             {
    #                 "einvoice_status_response": json.dumps(response, indent=2, default=str),
    #                 "einvoice_status": new_status if new_status in ("generated", "rejected") else move.einvoice_status,
    #                 "einvoice_peppol_status": peppol_status or move.einvoice_peppol_status,
    #                 "einvoice_peppol_mls_status": peppol_mls_status or move.einvoice_peppol_mls_status,
    #             }
    #         )
    #         self.env["einvoice.log"].create(
    #             {
    #                 "invoice_id": move.id,
    #                 "call_type": "status_check",
    #                 "request_payload": json.dumps({"reference": move.einvoice_reference}),
    #                 "response_payload": json.dumps(response, indent=2, default=str),
    #                 "status_code": str(response.get("status_code", "")),
    #                 "success": new_status in ("accepted", "rejected"),
    #             }
    #         )
    #     return True

    def action_send_to_einvoice(self):

        moves = self.filtered(lambda m: m.move_type in ("out_invoice", "out_refund")and m.state == "posted")
        if not moves:
            raise UserError("Select one or more customer invoices/credit notes first.")
        moves.filtered(lambda m: not m.einvoice_selected).write({
            "einvoice_selected": True,
            "einvoice_date_added": fields.Datetime.now(),
        })
        return True

    def action_remove_from_einvoice(self):

        for move in self:
            if move.einvoice_status not in ("draft"):
                raise UserError(
                    "Only Draft invoices can be removed from the "
                    "E-Invoice AR list. '%s' is currently '%s'."
                    % (move.name, dict(move._fields["einvoice_status"].selection).get(move.einvoice_status))
                )
        self.write({"einvoice_selected": False})
        return True

    def action_einvoice_view_logs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "E-Invoice API Logs",
            "res_model": "einvoice.log",
            "view_mode": "list,form",
            "domain": [("invoice_id", "=", self.id)],
        }


    def _einvoice_call_submit_api(self, json_payload):
        self.ensure_one()
        config = self.env["einvoice.config"].sudo().search([], limit=1)
        if not config:
            raise UserError(
                "E-Invoice API is not configured. Go to E-Invoice > "
                "Configuration and set up the Cygnet credentials first."
            )

        response = config._request(
            "POST", "document_save",
            data=json_payload,
            timeout=30,
        )
        response_data = response.json() if response.content else {}


        reference = (
                response_data.get("referenceId")
                or response_data.get("reference")
                or response_data.get("refId")
                or response_data.get("documentReferenceId")
        )

        return {
            "status_code": response.status_code,
            "reference": reference,
            "raw": response_data,
        }





    def _einvoice_call_status_api(self, reference):
        self.ensure_one()
        config = self.env["einvoice.config"].sudo().search([], limit=1)
        if not config:
            raise UserError(
                "E-Invoice API is not configured. Go to E-Invoice > "
                "Configuration and set up the Cygnet credentials first."
            )

        response = config._request(
            "GET", "status_check",
            params={"referenceId": reference},
            timeout=30,
        )
        response_data = response.json() if response.content else {}
        top_level_status = response_data.get("status")  # e.g. "P", "IP", "PE"
        data_report = (response_data.get("dataReport") or [{}])[0]
        document_uuid = data_report.get("documentUuid")
        einv_push_status = (data_report.get("eInv") or {}).get("pushStatus")
        peppol_push_status = (data_report.get("peppol") or {}).get("pushStatus")
        peppol_mls_push_status = (data_report.get("peppolMls") or {}).get("pushStatus")
        tax_authority_mls_status=(data_report.get("taxAuthorityMls") or {}).get("pushStatus")
        status = _map_einvoice_status(einv_push_status)

        # "PE" = validation error — no dataReport at all, the failure
        # details are under validationReport[].propertyErrors instead.
        # TODO: confirm other possible top-level codes besides
        # P / IP / PE and whether any of them should also map here.
        if status is None and top_level_status == "PE":
            status = "rejected"

        return {
            "status_code": response.status_code,
            "status":status,
            "document_uuid": document_uuid,
            "peppol_status": _map_peppol_status(peppol_push_status),
            "peppol_mls_status": _map_peppol_status(peppol_mls_push_status),
            "tax_authority_mls_status": _map_taxAuthorityMls_status(tax_authority_mls_status),
            "raw": response_data,
        }

