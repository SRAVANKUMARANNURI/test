# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import ValidationError

# file extension -> Cygnet referenceMimeCode. Only PDF is known so far.
EINV_ATTACHMENT_MIME_CODES = {"pdf": "1"}


class EinvoiceAttachment(models.Model):
    _name = "einvoice.attachment"
    _description = "E-Invoice Attachment"

    move_id = fields.Many2one("account.move", required=True, ondelete="cascade", index=True)
    description = fields.Char(string="Description", required=True,
                              help="Sent as referenceDescription, e.g. Delivery note")
    reference_number = fields.Char(string="Reference Number", required=True,
                                   help="Sent as referenceNumber, e.g. 1005")
    file = fields.Binary(string="File", required=True, attachment=True)
    file_name = fields.Char(string="File Name")

    @api.constrains("file_name")
    def _check_file_type(self):
        for rec in self:
            ext = (rec.file_name or "").rsplit(".", 1)[-1].lower()
            if ext not in EINV_ATTACHMENT_MIME_CODES:
                raise ValidationError(
                    "Unsupported file type '.%s'. Supported: %s"
                    % (ext, ", ".join(EINV_ATTACHMENT_MIME_CODES))
                )

    def _einv_payload_dict(self):
        self.ensure_one()
        ext = (self.file_name or "").rsplit(".", 1)[-1].lower()
        data = self.file or b""
        if isinstance(data, bytes):
            data = data.decode()
        return {
            "referenceScheme": "",
            "referenceType": "",
            "referenceDescription": self.description or "",
            "referenceNumber": self.reference_number or "",
            "referenceBinaryObject": data,
            "referenceName": self.file_name or "",
            "referenceMimeCode": EINV_ATTACHMENT_MIME_CODES[ext],
            "referenceLocation": "",
        }