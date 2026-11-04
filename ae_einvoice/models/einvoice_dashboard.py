# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError

# AR drill-down filters, applied on top of the period/company domain.
# "accepted" = einvoice_status "generated" (Cygnet accepted and generated it).
_AR_NOT_DRAFT = ("einvoice_status", "!=", "draft")
_AR_KEYS = {
    "all": [],
    "not_sent": [("einvoice_status", "=", "draft")],
    "sent": [_AR_NOT_DRAFT],
    "pending": [("einvoice_status", "=", "submitted")],
    "accepted": [("einvoice_status", "=", "generated")],
    "rejected": [("einvoice_status", "=", "rejected")],
    "peppol_pending": [_AR_NOT_DRAFT, ("einvoice_peppol_status", "=", "pending")],
    "peppol_accepted": [_AR_NOT_DRAFT, ("einvoice_peppol_status", "=", "accepted")],
    "peppol_rejected": [_AR_NOT_DRAFT, ("einvoice_peppol_status", "=", "rejected")],
    "fta_pending": [_AR_NOT_DRAFT, ("einvoice_tax_authority_mls_status", "=", "pending")],
    "fta_accepted": [_AR_NOT_DRAFT, ("einvoice_tax_authority_mls_status", "=", "accepted")],
    "fta_rejected": [_AR_NOT_DRAFT, ("einvoice_tax_authority_mls_status", "=", "rejected")],
}

# AP drill-down filters. "accepted" = turned into a vendor bill (matched or released).
_AP_KEYS = {
    "all": [],
    "pending": [("state", "in", ("lookup", "review"))],
    "accepted": [("state", "in", ("matched", "released"))],
    "billed": [("state", "=", "released")],
    "rejected": [("state", "=", "rejected")],
}


class EinvoiceDashboard(models.AbstractModel):
    _name = "einvoice.dashboard"
    _description = "E-Invoice Dashboard"

    @api.model
    def _period_start(self, period):
        today = fields.Date.context_today(self)
        if period == "month":
            return today.replace(day=1)
        if period == "30d":
            return fields.Date.subtract(today, days=30)
        if period == "year":
            return today.replace(month=1, day=1)
        return False  # "all"

    @api.model
    def _ar_domain(self, period):
        domain = [
            ("move_type", "in", ("out_invoice", "out_refund")),
            ("einvoice_selected", "=", True),
            ("company_id", "in", self.env.companies.ids),
        ]
        start = self._period_start(period)
        if start:
            domain.append(("invoice_date", ">=", start))
        return domain

    @api.model
    def _ap_domain(self, period):
        domain = [("company_id", "in", self.env.companies.ids)]
        start = self._period_start(period)
        if start:
            domain.append(("document_date", ">=", start))
        return domain

    @api.model
    def _count_by(self, model, domain, field_name):
        groups = self.env[model]._read_group(domain, [field_name], ["__count"])
        return {value: count for value, count in groups}

    @staticmethod
    def _rate(accepted, rejected):
        decided = accepted + rejected
        return round(accepted * 100.0 / decided) if decided else None

    @api.model
    def get_data(self, period="month"):
        # --- AR: invoices pushed to Cygnet from Customer Invoices ---
        ar_domain = self._ar_domain(period)
        by_status = self._count_by("account.move", ar_domain, "einvoice_status")
        pending = by_status.get("submitted", 0)
        accepted = by_status.get("generated", 0)
        rejected = by_status.get("rejected", 0)
        not_sent = by_status.get("draft", 0)
        sent = pending + accepted + rejected
        sent_domain = ar_domain + [_AR_NOT_DRAFT]
        peppol = self._count_by("account.move", sent_domain, "einvoice_peppol_status")
        fta = self._count_by("account.move", sent_domain, "einvoice_tax_authority_mls_status")

        # --- AP: supplier invoices fetched from Cygnet ---
        by_state = self._count_by("einvoice.purchase", self._ap_domain(period), "state")
        ap_pending = by_state.get("lookup", 0) + by_state.get("review", 0)
        ap_accepted = by_state.get("matched", 0) + by_state.get("released", 0)
        ap_rejected = by_state.get("rejected", 0)

        company = self.env.company
        return {
            "period": period,
            "ar": {
                "total": sent + not_sent,
                "not_sent": not_sent,
                "sent": sent,
                "pending": pending,
                "accepted": accepted,
                "rejected": rejected,
                "rate": self._rate(accepted, rejected),
                "peppol": {k: peppol.get(k, 0) for k in ("pending", "accepted", "rejected")},
                "fta": {k: fta.get(k, 0) for k in ("pending", "accepted", "rejected")},
            },
            "ap": {
                "fetched": ap_pending + ap_accepted + ap_rejected,
                "pending": ap_pending,
                "accepted": ap_accepted,
                "billed": by_state.get("released", 0),
                "rejected": ap_rejected,
                "rate": self._rate(ap_accepted, ap_rejected),
                "last_fetched_up_to": fields.Datetime.to_string(company.einvoice_ap_fetched_up_to)
                if company.einvoice_ap_fetched_up_to else False,
            },
        }

    @api.model
    def open_list(self, side, key, period="month"):
        """Return the AR/AP list action filtered to the dashboard bucket clicked."""
        if side == "ar":
            extra, domain = _AR_KEYS.get(key), self._ar_domain(period)
            xmlid = "ae_einvoice.action_einvoice_ar"
        elif side == "ap":
            extra, domain = _AP_KEYS.get(key), self._ap_domain(period)
            xmlid = "ae_einvoice.action_einvoice_ap"
        else:
            raise UserError("Unknown dashboard section.")
        if extra is None:
            raise UserError("Unknown dashboard filter.")
        action = self.env["ir.actions.actions"]._for_xml_id(xmlid)
        action["domain"] = domain + extra
        return action
