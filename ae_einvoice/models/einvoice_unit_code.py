# -*- coding: utf-8 -*-
from odoo import api, fields, models


class EinvoiceUnitCode(models.Model):
    """Unit of measure codes (UN/ECE Recommendation 20) from the Cygnet
    Masters sheet. Loaded from data/einvoice.unit.code.csv."""
    _name = "einvoice.unit.code"
    _description = "E-Invoice Unit of Measure Code"
    _order = "code"
    _rec_names_search = ["code", "name"]

    code = fields.Char(required=True, index=True)
    name = fields.Char()

    @api.depends("code", "name")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s - %s" % (rec.code, rec.name) if rec.name else rec.code
