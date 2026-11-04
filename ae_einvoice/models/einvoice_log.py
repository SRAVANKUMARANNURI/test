# -*- coding: utf-8 -*-
import logging

from odoo import SUPERUSER_ID, api, fields, models

_logger = logging.getLogger(__name__)


class EinvoiceLog(models.Model):

    _name = "einvoice.log"
    _description = "E-Invoice API Call Log"
    _order = "create_date desc"

    direction = fields.Selection(
        [("ar", "Sales (AR)"), ("ap", "Purchases (AP)")],
        string="Side",
        default="ar",
        required=True,
        index=True,
    )
    # AR logs link to the invoice; AP lookups are company-level, so no link.
    invoice_id = fields.Many2one(
        "account.move",
        string="Invoice",
        ondelete="cascade",
        index=True,
    )
    # Plain integer (not a Many2one) on purpose: the AP flow locks the purchase
    # row with SELECT ... FOR UPDATE, and a foreign key insert from the separate
    # log transaction would wait on that lock.
    purchase_res_id = fields.Integer(string="AP Invoice ID", index=True)
    reference = fields.Char(string="Reference")
    company_id = fields.Many2one(
        "res.company", string="Company", default=lambda self: self.env.company, index=True
    )
    call_type = fields.Selection(
        [
            ("submit", "Submit"),
            ("status_check", "Status Check"),
            ("ap_lookup", "Purchase Lookup"),
            ("ap_detail", "Purchase Details"),
        ],
        string="Call Type",
        required=True,
    )
    request_payload = fields.Text(string="Request Payload (JSON)")
    response_payload = fields.Text(string="Response Payload (JSON)")
    status_code = fields.Char(string="HTTP Status Code")
    success = fields.Boolean(string="Success", default=False)
    call_datetime = fields.Datetime(
        string="Call Time", default=fields.Datetime.now, required=True
    )

    @api.depends("invoice_id", "reference", "call_type")
    def _compute_display_name(self):
        types = dict(self._fields["call_type"].selection)
        for log in self:
            label = log.invoice_id.display_name or log.reference
            call = types.get(log.call_type, "")
            log.display_name = "%s - %s" % (call, label) if label else call

    @api.model
    def _write_log(self, vals):
        """Create a log row in its own transaction.

        The AP flows raise UserError after a failed call, which rolls back the
        caller's transaction. A separate cursor keeps the audit row anyway,
        without committing the caller's half-finished work. Logging must never
        break the API call itself, so any failure here is only reported.
        """
        try:
            with self.env.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                env["einvoice.log"].create(vals)
        except Exception:
            _logger.exception("Could not write the E-Invoice API log")