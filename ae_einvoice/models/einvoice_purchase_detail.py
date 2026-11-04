import json
import math

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from .cygnet_api_errors import api_error_message, response_json
from .account_move import _map_peppol_status, _map_taxAuthorityMls_status

def populated_values(value):
    """Remove null/empty structures while preserving meaningful zero/false values."""
    if isinstance(value, dict):
        return {k: cleaned for k, v in value.items()
                if (cleaned := populated_values(v)) is not None} or None
    if isinstance(value, list):
        return [cleaned for v in value if (cleaned := populated_values(v)) is not None] or None
    return None if value is None or value == "" else value


def detail_rows(value, prefix=""):
    rows = []
    if isinstance(value, dict):
        for key, child in value.items():
            rows.extend(detail_rows(child, f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, list):
        for index, child in enumerate(value, 1):
            rows.extend(detail_rows(child, f"{prefix}[{index}]"))
    elif value is not None:
        rows.append({"path": prefix, "value": str(value)})
    return rows


def invoice_number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise UserError("Cygnet returned an invalid numeric value for %s." % label)
    return value


class EinvoicePurchaseDetail(models.Model):
    _inherit = "einvoice.purchase"

    state = fields.Selection([("lookup", "Lookup"), ("review", "Review"),
                              ("matched", "Match APs"), ("released", "Vendor Bills"), ("rejected", "Rejected")],
                             default="lookup", required=True, readonly=True, copy=False)
    partner_id = fields.Many2one("res.partner", string="Vendor", check_company=True)
    supplier_legal_name = fields.Char(string="Supplier Legal Name", compute="_compute_supplier_details")
    supplier_legal_reg_no = fields.Char(string="Supplier Legal Registration", compute="_compute_supplier_details")
    supplier_endpoint = fields.Char(string="Supplier Endpoint", compute="_compute_supplier_details")
    currency_id = fields.Many2one("res.currency", string="Currency")
    invoice_date = fields.Date(string="Bill Date")
    due_date = fields.Date()
    purchase_order_id = fields.Many2one("purchase.order", check_company=True, string="Purchase Order")
    bill_id = fields.Many2one("account.move", readonly=True, copy=False, check_company=True, ondelete="restrict")
    detail_payload = fields.Text(readonly=True, copy=False)
    detail_fetched_at = fields.Datetime(readonly=True)
    document_uuid = fields.Char(string="Document UUID", readonly=True, copy=False)
    generated_date = fields.Datetime(string="Generated Date", readonly=True, copy=False)
    tax_authority_transmission_date = fields.Datetime(
        string="Tax Authority Transmission Date", readonly=True, copy=False)
    peppol_mls_status = fields.Selection(
        [
            ("draft", "Draft"),
            ("pending", "Pending"),
            ("rejected", "Rejected"),
            ("accepted", "Accepted"),
        ],
        string="Peppol MLS Status", default="draft", readonly=True, copy=False,
    )
    tax_authority_mls_status = fields.Selection(
        [
            ("draft", "Draft"),
            ("pending", "Pending"),
            ("rejected", "Rejected"),
            ("accepted", "Accepted"),
        ],
        string="Tax Authority MLS Status", default="draft", readonly=True, copy=False,
    )
    expected_discount = fields.Monetary(string="Invoice Discount Amount", currency_field="currency_id")
    expected_untaxed = fields.Monetary(string="Invoice Untaxed Amount", currency_field="currency_id")
    expected_tax = fields.Monetary(string="Invoice Tax Amount", currency_field="currency_id")
    expected_total = fields.Monetary(string="Invoice Total", currency_field="currency_id")
    line_ids = fields.One2many("einvoice.purchase.line", "purchase_id", string="Invoice Lines", copy=False)
    detail_value_ids = fields.One2many("einvoice.purchase.value", "purchase_id", string="Other Populated Details", copy=False)

    @api.depends("detail_payload")
    def _compute_supplier_details(self):
        for record in self:
            doc = json.loads(record.detail_payload or "{}")
            supplier = doc.get("billFrom") or {}
            record.supplier_legal_name = supplier.get("legalName") or False
            record.supplier_legal_reg_no = supplier.get("legalRegNo") or False
            record.supplier_endpoint = supplier.get("endpoint") or False

    @api.constrains("partner_id", "supplier_tax_reg_no", "company_id")
    def _check_supplier_partner(self):
        for record in self:
            if record.partner_id and (
                record.partner_id.commercial_partner_id == record.company_id.partner_id.commercial_partner_id
                or record.partner_id.commercial_partner_id.vat != record.supplier_tax_reg_no
            ):
                raise ValidationError("Select the supplier identified by billFrom.taxRegNo, not the buyer company.")

    def write(self, vals):
        if any(record.state == "rejected" for record in self):
            raise UserError("Rejected AP invoices cannot be edited.")
        if {"state", "bill_id"}.intersection(vals):
            raise UserError("Use the AP workflow buttons to change the processing stage or linked bill.")
        review_fields = {"partner_id", "currency_id", "invoice_date", "due_date", "purchase_order_id","expected_discount",
                         "expected_untaxed", "expected_tax", "expected_total", "line_ids", "detail_value_ids"}
        if review_fields.intersection(vals) and any(r.state in ("matched", "released") for r in self):
            raise UserError("Converted AP invoice details cannot be edited.")
        return super().write(vals)

    def _write_workflow(self, vals):
        if any(record.state == "rejected" for record in self):
            raise UserError("Rejected AP invoices cannot be changed.")
        return super().write(vals)

    def unlink(self):
        if any(record.state == "rejected" for record in self):
            raise UserError("Rejected AP invoices cannot be deleted.")
        return super().unlink()

    def action_refuse(self):
        for record in self:
            record._lock_for_conversion()
            if record.state not in ("review", "matched"):
                raise UserError("Only invoices under review or in Match APs can be refused.")
            if record.bill_id:
                if record.bill_id.state != "draft":
                    raise UserError("Only draft bills can be refused during AP matching.")
                record.bill_id.button_cancel()
            record._write_workflow({"state": "rejected"})
        return True

    einvoice_log_count = fields.Integer(string="API Logs", compute="_compute_einvoice_log_count")

    def _compute_einvoice_log_count(self):
        groups = self.env["einvoice.log"]._read_group(
            [("purchase_res_id", "in", self.ids)], ["purchase_res_id"], ["__count"])
        counts = dict(groups)
        for record in self:
            record.einvoice_log_count = counts.get(record.id, 0)

    def action_view_logs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "E-Invoice API Logs",
            "res_model": "einvoice.log",
            "view_mode": "list,form",
            "domain": [("purchase_res_id", "=", self.id)],
        }

    def _lock_for_conversion(self):
        self.ensure_one()
        self.check_access("write")
        self.env.cr.execute("SELECT id FROM einvoice_purchase WHERE id = %s FOR UPDATE", [self.id])
        self.invalidate_recordset(["state", "bill_id"])

    def action_view_details(self):
        self.ensure_one()
        self._lock_for_conversion()
        if not self.detail_fetched_at and self.state != "rejected":
            self._fetch_purchase_details()
        return {"type": "ir.actions.act_window", "name": "AP Invoice Details",
                "res_model": self._name, "res_id": self.id, "view_mode": "form",
                "views": [(self.env.ref("ae_einvoice.view_einvoice_purchase_detail_form").id, "form")]}

    def _fetch_purchase_details(self):
        self.ensure_one()
        config = self.env["einvoice.config"].sudo().search([], limit=1)
        if not config:
            raise UserError("Configure the Cygnet connection first.")
        lookup = json.loads(self.lookup_payload)
        keys = ("country", "entityTaxRegNo", "entityTin", "entityName", "supplyType",
                "documentType", "documentNumber", "documentDate", "billFromTaxRegNo")
        try:
            response = config._request_logged("ap_detail", "POST", "purchase_detail",
                                              log_vals={"company_id": self.company_id.id,
                                                        "purchase_res_id": self.id,
                                                        "reference": self.document_number},
                                              json={"criterias": [{k: lookup.get(k) for k in keys}],
                                                    "downloadAdditionalAttachment": "false"},
                                              timeout=30)
        except requests.RequestException as error:
            raise UserError(api_error_message(
                "Cygnet purchase details failed for %s" % self.document_number, error=error)) from error
        operation = "Cygnet purchase details failed for %s" % self.document_number
        data = response_json(response, operation)
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise UserError(api_error_message(
                "%s: expected one purchase document" % operation, response=response))
        doc = data[0]
        if not doc.get("documentNumber"):
            raise UserError(api_error_message(operation, response=response))
        if (doc.get("documentNumber") != self.document_number
                or doc.get("entityTaxRegNo") != self.entity_tax_reg_no
                or doc.get("documentType") != self.document_type
                or (doc.get("billFrom") or {}).get("taxRegNo") != self.supplier_tax_reg_no):
            raise UserError("Cygnet details do not match the selected invoice.")
        items = doc.get("documentItems")
        if not isinstance(items, list) or not items or not all(isinstance(x, dict) for x in items):
            raise UserError("The document has no valid invoice lines.")
        currency = self.env["res.currency"].search([("name", "=", doc.get("documentCurrency"))], limit=1)
        if not currency:
            raise UserError("Activate the invoice currency in Odoo before retrieving details.")
        partners = self.env["res.partner"].search([
            ("vat", "=", doc["billFrom"]["taxRegNo"]), ("parent_id", "=", False),
            ("id", "!=", self.company_id.partner_id.commercial_partner_id.id),
            ("company_id", "in", [False, self.company_id.id])], limit=2)
        taxes = self.env["account.tax"].with_company(self.company_id)
        lines = []
        total_discount = 0.0
        for item in items:
            base = invoice_number(item.get("priceBaseQuantity", 1), "price base quantity")
            if base <= 0:
                raise UserError("The price base quantity must be greater than zero.")
            # Gross price; the discount is kept in its own column so the bill
            # line does not discount twice.
            price = item.get("unitPrice")
            if price is None:
                price = item.get("netUnitPrice", 0)
            price = invoice_number(price, "unit price")
            quantity = invoice_number(item.get("invoicedQuantity"), "invoiced quantity")
            gross_line = price / base * quantity
            details = item.get("allowances") or item.get("allowanceDetails") or []
            discount_amount = invoice_number(item.get("unitAllowanceAmount") or 0, "unit allowance") / base * quantity
            discount_amount += sum(invoice_number(d.get("allowanceAmount") or 0, "allowance amount") for d in details)
            if len(details) == 1 and details[0].get("allowancePercentage") is not None:
                discount_pct = invoice_number(details[0]["allowancePercentage"], "allowance percentage")
            else:
                discount_pct = discount_amount / gross_line * 100 if gross_line else 0.0
            discount_pct = max(0.0, min(discount_pct, 100.0))
            total_discount += discount_amount
            candidates = taxes.search([("company_id", "=", self.company_id.id),
                                       ("type_tax_use", "=", "purchase"), ("amount_type", "=", "percent"),
                                       ("amount", "=", item.get("taxRate", 0)),
                                       ("price_include", "=", False)], order="sequence, id", limit=1)
            lines.append(fields.Command.create({
                "name": item.get("description") or item.get("name") or "Invoice line",
                "quantity": quantity, "price_unit": price / base,
                "discount": discount_pct,
                "source_unit_code": item.get("invoicedQuantityUnitCode"),
                "tax_ids": [fields.Command.set(candidates.ids)],
                "source_tax_rate": item.get("taxRate", 0),
            }))
        try:
            invoice_date = fields.Date.to_date(doc["documentDate"][:10])
            due_date = fields.Date.to_date(doc["paymentDueDate"][:10]) if doc.get("paymentDueDate") else False
        except (KeyError, TypeError, ValueError):
            raise UserError("Cygnet returned an invalid invoice or due date.")
        cleaned = populated_values(doc)
        # Accounting values have dedicated controls; remaining values retain their API paths.
        other = {k: v for k, v in cleaned.items() if k not in {
            "documentItems", "documentDate", "paymentDueDate", "documentCurrency",
            "invoiceTotalAmountWithoutTax", "invoiceTotalTaxAmount", "totalDocumentAmount"}}
        # Keep populated line metadata too, including tax amounts and price-base information.
        other["documentItems"] = cleaned.get("documentItems", [])
        einv = doc.get("eInv") or {}
        generated_date = einv.get("generatedDate")
        tax_authority_transmission_date = einv.get("taxAuthorityTransmissionDate")
        peppol_mls_status = _map_peppol_status((doc.get("peppolMls") or {}).get("pushStatus"))
        tax_authority_mls_status = _map_taxAuthorityMls_status(
            (doc.get("taxAuthorityMls") or {}).get("pushStatus"))
        self._write_workflow({"state": "review", "partner_id": partners.id if len(partners) == 1 else False,
                    "currency_id": currency.id, "invoice_date": invoice_date, "due_date": due_date,
                    "expected_untaxed": invoice_number(doc.get("invoiceTotalAmountWithoutTax"), "untaxed total"),
                    "expected_tax": invoice_number(doc.get("invoiceTotalTaxAmount"), "tax total"),
                    "expected_total": invoice_number(doc.get("totalDocumentAmount"), "invoice total"),
                              "expected_discount": total_discount + invoice_number(
                                  doc.get("totalDocumentAllowance") or 0, "document allowance"),
                    "document_uuid": doc.get("documentUuid"),
                    "generated_date": generated_date,
                    "tax_authority_transmission_date": tax_authority_transmission_date,
                    "peppol_mls_status": peppol_mls_status,
                    "tax_authority_mls_status": tax_authority_mls_status,
                    "line_ids": [fields.Command.clear()] + lines,
                    "detail_value_ids": [fields.Command.clear()] + [fields.Command.create(v) for v in detail_rows(other)],
                    "detail_payload": json.dumps(cleaned, indent=2), "detail_fetched_at": fields.Datetime.now()})

    @api.constrains("purchase_order_id", "partner_id", "currency_id")
    def _check_purchase_order(self):
        for record in self:
            po = record.purchase_order_id
            if po and (po.company_id != record.company_id
                       or po.partner_id.commercial_partner_id != record.partner_id.commercial_partner_id
                       or po.currency_id != record.currency_id or po.state not in ("purchase", "done")):
                raise ValidationError("Select a confirmed purchase order with the same company, vendor and currency.")

    def action_convert_to_bill(self):
        self.ensure_one()
        self._lock_for_conversion()
        if self.state == "rejected":
            raise UserError("Rejected AP invoices cannot be converted to bills.")
        if self.bill_id:
            return self.action_open_bill()
        if self.state != "review" or not self.line_ids or not self.partner_id or not self.currency_id or not self.invoice_date:
            raise UserError("Fetch the details and select a vendor, currency, bill date and invoice lines first.")
        if self.document_type not in ("INV", "CRN"):
            raise UserError("Only invoices and credit notes can be converted to vendor bills.")
        if self.partner_id.commercial_partner_id.vat != self.supplier_tax_reg_no:
            raise UserError("The selected vendor's tax registration number must match the supplier TRN.")
        self._check_purchase_order()
        moves = self.env["account.move"].with_company(self.company_id)
        move_type = "in_invoice" if self.document_type == "INV" else "in_refund"
        if moves.search_count([("company_id", "=", self.company_id.id), ("move_type", "=", move_type),
                               ("commercial_partner_id", "=", self.partner_id.commercial_partner_id.id),
                               ("ref", "=", self.document_number), ("state", "!=", "cancel")]):
            raise UserError("A vendor bill with this supplier reference already exists.")
        commands = []
        for line in self.line_ids:
            if not line.account_id or line.quantity <= 0 or not math.isfinite(line.quantity) or not math.isfinite(line.price_unit):
                raise UserError("Each line needs an expense account, positive quantity and valid unit price.")
            if line.account_id.account_type in ("asset_receivable", "liability_payable", "off_balance"):
                raise UserError("Invoice lines cannot use receivable, payable or off-balance accounts.")
            if line.source_tax_rate and not line.tax_ids:
                raise UserError("Select the purchase tax for every taxable line.")
            commands.append(fields.Command.create({
                "name": line.name, "product_id": line.product_id.id,
                "account_id": line.account_id.id, "quantity": line.quantity,
                "price_unit": line.price_unit, "tax_ids": [fields.Command.set(line.tax_ids.ids)],
                "discount": line.discount,
            }))
        bill = moves.create({"move_type": move_type, "company_id": self.company_id.id,
                             "partner_id": self.partner_id.id, "currency_id": self.currency_id.id,
                             "invoice_date": self.invoice_date, "invoice_date_due": self.due_date,
                             "ref": self.document_number, "invoice_origin": self.purchase_order_id.name or self.document_number,
                             "einvoice_ap_id": self.id, "invoice_line_ids": commands})
        for actual, expected in ((bill.amount_untaxed, self.expected_untaxed),
                                 (bill.amount_tax, self.expected_tax), (bill.amount_total, self.expected_total)):
            if self.currency_id.compare_amounts(actual, expected):
                raise UserError("The calculated bill totals do not match the reviewed invoice totals. Check quantities, prices and taxes before converting.")
        self._write_workflow({"bill_id": bill.id, "state": "matched"})
        return self.env["ir.actions.actions"]._for_xml_id("ae_einvoice.action_einvoice_match_ap")

    def action_open_bill(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "account.move",
                "res_id": self.bill_id.id, "view_mode": "form"}

    def action_move_to_vendor_bills(self):
        for record in self:
            record._lock_for_conversion()
            if record.state != "matched" or not record.bill_id or record.bill_id.state != "draft":
                raise UserError("Only converted draft bills in Match APs can be moved to Vendor Bills.")
            bill = record.bill_id
            if (bill.company_id != record.company_id or bill.currency_id != record.currency_id
                    or bill.partner_id.commercial_partner_id != record.partner_id.commercial_partner_id
                    or bill.ref != record.document_number
                    or bill.move_type != ("in_invoice" if record.document_type == "INV" else "in_refund")):
                raise UserError("The draft bill no longer matches the AP invoice company, currency, vendor or reference.")
            for actual, expected in ((bill.amount_untaxed, record.expected_untaxed),
                                     (bill.amount_tax, record.expected_tax), (bill.amount_total, record.expected_total)):
                if record.currency_id.compare_amounts(actual, expected):
                    raise UserError("The draft bill totals changed. Reconcile them with the AP invoice before moving it to Vendor Bills.")
            record._write_workflow({"state": "released"})
        if len(self) == 1:
            return self.action_open_bill()
        return True


class EinvoicePurchaseLine(models.Model):
    _name = "einvoice.purchase.line"
    _description = "AP Invoice Review Line"
    _check_company_auto = True

    purchase_id = fields.Many2one("einvoice.purchase", required=True, ondelete="cascade")
    company_id = fields.Many2one(related="purchase_id.company_id", store=True)
    name = fields.Char(required=True)
    product_id = fields.Many2one("product.product", check_company=True)
    account_id = fields.Many2one("account.account", check_company=True)
    quantity = fields.Float(default=1, digits="Product Unit of Measure")
    price_unit = fields.Float(digits="Product Price")
    discount = fields.Float(string="Discount (%)", digits="Discount")
    source_unit_code = fields.Char(readonly=True)
    source_tax_rate = fields.Float(readonly=True)
    tax_ids = fields.Many2many("account.tax", check_company=True)

    @api.model_create_multi
    def create(self, vals_list):
        parents = self.env["einvoice.purchase"].browse([v.get("purchase_id") for v in vals_list if v.get("purchase_id")])
        if any(p.state in ("matched", "released", "rejected") for p in parents):
            raise UserError("Converted AP invoice lines cannot be changed.")
        return super().create(vals_list)

    def write(self, vals):
        if any(line.purchase_id.state in ("matched", "released", "rejected") for line in self):
            raise UserError("Converted AP invoice lines cannot be changed.")
        if "purchase_id" in vals:
            raise UserError("Invoice lines cannot be moved between AP invoices.")
        return super().write(vals)

    def unlink(self):
        if any(line.purchase_id.state in ("matched", "released", "rejected") for line in self):
            raise UserError("Converted AP invoice lines cannot be removed.")
        return super().unlink()

    @api.onchange("product_id")
    def _onchange_product_id(self):
        if self.product_id:
            product = self.product_id.with_company(self.company_id)
            self.account_id = product.property_account_expense_id or product.categ_id.property_account_expense_categ_id


class EinvoicePurchaseValue(models.Model):
    _name = "einvoice.purchase.value"
    _description = "Populated AP Invoice Detail"
    _order = "id"

    purchase_id = fields.Many2one("einvoice.purchase", required=True, ondelete="cascade")
    company_id = fields.Many2one(related="purchase_id.company_id", store=True)
    path = fields.Char(readonly=True, required=True)
    value = fields.Text()

    @api.model_create_multi
    def create(self, vals_list):
        parents = self.env["einvoice.purchase"].browse([v["purchase_id"] for v in vals_list if v.get("purchase_id")])
        if any(p.state == "rejected" for p in parents):
            raise UserError("Rejected AP invoice details cannot be changed.")
        return super().create(vals_list)

    def write(self, vals):
        if any(r.purchase_id.state == "rejected" for r in self):
            raise UserError("Rejected AP invoice details cannot be changed.")
        if "purchase_id" in vals:
            raise UserError("Details cannot be moved between AP invoices.")
        return super().write(vals)

    def unlink(self):
        if any(r.purchase_id.state == "rejected" for r in self):
            raise UserError("Rejected AP invoice details cannot be deleted.")
        return super().unlink()


class AccountMoveAP(models.Model):
    _inherit = "account.move"

    einvoice_ap_id = fields.Many2one("einvoice.purchase", string="AP E-Invoice", copy=False, readonly=True, check_company=True, ondelete="restrict")
    einvoice_ap_state = fields.Selection(string="AP Processing Stage", related="einvoice_ap_id.state", store=True)
    _einvoice_ap_unique = models.Constraint("UNIQUE(einvoice_ap_id)", "This AP invoice already has a bill.")

    def write(self, vals):
        if any(move.einvoice_ap_id.state == "rejected" for move in self):
            raise UserError("Bills linked to rejected AP invoices cannot be edited.")
        return super().write(vals)

    def unlink(self):
        if any(move.einvoice_ap_id.state == "rejected" for move in self):
            raise UserError("Bills linked to rejected AP invoices cannot be deleted.")
        return super().unlink()

    def _post(self, soft=True):
        if any(move.einvoice_ap_id and move.einvoice_ap_state != "released" for move in self):
            raise UserError("Move this bill from Match APs to Vendor Bills before posting it.")
        return super()._post(soft=soft)


class AccountMoveLineAP(models.Model):
    _inherit = "account.move.line"

    @api.model_create_multi
    def create(self, vals_list):
        moves = self.env["account.move"].browse([v["move_id"] for v in vals_list if v.get("move_id")])
        if any(move.einvoice_ap_id.state == "rejected" for move in moves):
            raise UserError("Rejected AP bill lines cannot be changed.")
        return super().create(vals_list)

    def write(self, vals):
        moves = self.move_id
        if vals.get("move_id"):
            moves |= self.env["account.move"].browse(vals["move_id"])
        if any(move.einvoice_ap_id.state == "rejected" for move in moves):
            raise UserError("Rejected AP bill lines cannot be changed.")
        return super().write(vals)

    def unlink(self):
        if any(line.move_id.einvoice_ap_id.state == "rejected" for line in self):
            raise UserError("Rejected AP bill lines cannot be deleted.")
        return super().unlink()
