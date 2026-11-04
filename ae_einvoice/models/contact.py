# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import ValidationError

EINVOICE_MANDATORY_FIELDS = [
    ("name", "Name"),
    ("vat", "Tax ID (VAT)"),
    ("street", "Street"),
    ("city", "City"),
    ("state_id", "State"),
    ("country_id", "Country"),
]


class ResPartner(models.Model):
    _inherit = "res.partner"

    einvoice_eligible = fields.Boolean(
        string="Eligible for E-Invoice",
        help="When enabled, this partner's e-invoice fields (Name, VAT, "
             "Street, City, State, Country) become mandatory.",
    )

    @api.constrains("einvoice_eligible", "name", "vat", "street", "city", "state_id", "country_id")
    def _check_einvoice_mandatory_fields(self):
        for partner in self:
            if not partner.einvoice_eligible:
                continue
            missing = [
                label for fname, label in EINVOICE_MANDATORY_FIELDS
                if not partner[fname]
            ]
            if missing:
                raise ValidationError(
                    "This contact is marked 'Eligible for E-Invoice', so the "
                    "following fields are mandatory: %s" % ", ".join(missing)
                )