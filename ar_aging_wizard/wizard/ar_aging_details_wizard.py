# -*- coding: utf-8 -*-
import base64
import io

from odoo import api, fields, models
from odoo.exceptions import UserError

try:
    import xlsxwriter
except ImportError:
    xlsxwriter = None


class ArAgingDetailsWizard(models.TransientModel):
    _name = 'ar.aging.details.wizard'
    _description = 'AR Aging Details by Invoice Due Date - Export Wizard'

    as_of_date = fields.Date(
        string='As of Date',
        required=True,
        default=fields.Date.context_today,
        help="Aging is computed against this date (age = as_of_date - due_date).",
    )
    date_from = fields.Date(
        string='From Date',
        help="Optional. Leave empty to include everything up to As of Date. "
             "Set this to narrow the range, e.g. only the last 2 or 5 months.",
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
    )
    partner_ids = fields.Many2many(
        'res.partner',
        string='Customers',
        help="Leave empty to include all customers.",
    )
    include_credit_memos = fields.Boolean(
        string='Include Credit Memos',
        default=True,
    )
    aed_fixed_rate = fields.Float(
        string='Fixed AED Rate',
        default=3.6725,
        help="Fixed rate used to convert USD balances to AED for the "
             "'Amount in AED' column (AED is pegged to USD). AED balances "
             "pass through at 1:1; other currencies fall back to the "
             "exchange rate recorded on the invoice.",
    )

    HEADERS = [
        'date',
        'status',
        'entity',
        'age',
        'due_date',
        'transaction_number/Invoice #',
        'customer_name',
        'currency_code',
        'balance',
        'amount',
        'exchange_rate',
        'Amount in AED',
    ]

    def _get_move_lines(self):
        self.ensure_one()
        domain = [
            ('account_id.account_type', '=', 'asset_receivable'),
            ('parent_state', '=', 'posted'),
            ('reconciled', '=', False),
            ('company_id', '=', self.company_id.id),
            ('date', '<=', self.as_of_date),
            # Some "Unbilled" journal entries post vendor-side accruals to
            # an account that is ALSO typed asset_receivable, which pulls
            # vendor lines into this report. A genuine customer accrual
            # (e.g. debit 141010 Accrued Revenue) always pairs against an
            # Income-type account (420100-420400 Unbilled Revenue, etc.)
            # on the same move; a vendor accrual pairs against an Expense
            # account instead. Checking the sibling line's account type is
            # a more reliable customer/vendor signal than partner flags,
            # which aren't always set on manual journal entries.
            ('move_id.line_ids.account_id.account_type', 'in', ['income', 'income_other']),
        ]
        if self.date_from:
            domain.append(('date', '>=', self.date_from))
        if not self.include_credit_memos:
            domain.append(('move_id.move_type', '!=', 'out_refund'))
        if self.partner_ids:
            domain.append(('partner_id', 'in', self.partner_ids.ids))

        return self.env['account.move.line'].search(domain, order='date_maturity asc, move_id asc')

    def _line_values(self, line):
        move = line.move_id
        company_currency = self.company_id.currency_id
        line_currency = line.currency_id or company_currency

        # Balance / amount are kept in the INVOICE currency (not converted),
        # matching the source report's behaviour.
        if line.currency_id and line.currency_id != company_currency:
            balance = line.amount_residual_currency
        else:
            balance = line.amount_residual
        amount = move.amount_total

        # Rate Odoo recorded at posting time. VERIFY the direction (this
        # field's semantics changed across versions - confirm against a
        # known invoice before relying on it for downstream conversion).
        exchange_rate = move.invoice_currency_rate or 1.0

        balance_abs = abs(balance) if balance else 0.0

        # Fixed-rate AED conversion: AED passes through 1:1, USD uses the
        # fixed peg rate, anything else falls back to the dynamic rate
        # recorded on the invoice (best available for non-pegged currencies).
        if line_currency.name == 'AED':
            aed_amount = balance_abs
        elif line_currency.name == 'USD':
            aed_amount = balance_abs * self.aed_fixed_rate
        else:
            aed_amount = balance_abs * exchange_rate

        # Miscellaneous journal entries (e.g. Unbilled - T&M postings)
        # have no invoice_date_due / invoice_date, so fall back to the
        # accounting date on the move.
        due_date = line.date_maturity or move.invoice_date_due or move.invoice_date or move.date
        inv_date = move.invoice_date or move.date

        age = (self.as_of_date - due_date).days if due_date else False
        status = 'overdue' if (due_date and due_date < self.as_of_date) else 'current'

        if move.move_type == 'out_refund':
            entity = 'credit_memo'
        elif move.move_type == 'out_invoice':
            entity = 'invoice'
        else:
            # Plain journal entry, e.g. Unbilled - T&M
            entity = 'unbilled'

        # For plain journal entries (e.g. Unbilled - T&M postings), the
        # partner often lives on the journal LINE rather than the move
        # header, so move.partner_id can be blank. Prefer the line's
        # partner and fall back to the move's.
        partner = line.partner_id or move.partner_id
        customer_name = partner.commercial_partner_id.name or partner.name or ''

        return {
            'date': inv_date,
            'status': status,
            'entity': entity,
            'age': age,
            'due_date': due_date,
            'transaction_number': move.name,
            'customer_name': customer_name,
            'currency_code': line_currency.name,
            'balance': balance_abs,
            'amount': abs(amount) if amount else 0.0,
            'exchange_rate': exchange_rate,
            'aed_amount': aed_amount,
        }

    def action_export_xlsx(self):
        self.ensure_one()
        if xlsxwriter is None:
            raise UserError(
                "The 'xlsxwriter' Python library is not installed on this "
                "server. Install it with: pip install xlsxwriter"
            )

        lines = self._get_move_lines()

        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet('AR Aging Details By Invoice Due')

        header_fmt = workbook.add_format({
            'bold': True, 'bg_color': '#D9D9D9', 'border': 1,
        })
        date_fmt = workbook.add_format({'num_format': 'yyyy-mm-dd'})
        num_fmt = workbook.add_format({'num_format': '#,##0.00'})

        for col, header in enumerate(self.HEADERS):
            sheet.write(0, col, header, header_fmt)
            sheet.set_column(col, col, max(14, len(header) + 2))

        row = 1
        for line in lines:
            vals = self._line_values(line)
            if not vals['balance']:
                continue  # skip fully-settled residuals rounding to 0

            col = 0
            sheet.write_datetime(row, col, vals['date'], date_fmt) if vals['date'] else sheet.write_blank(row, col, None)
            col += 1
            sheet.write(row, col, vals['status']); col += 1
            sheet.write(row, col, vals['entity']); col += 1
            sheet.write_number(row, col, vals['age'] if vals['age'] is not False else 0); col += 1
            sheet.write_datetime(row, col, vals['due_date'], date_fmt) if vals['due_date'] else sheet.write_blank(row, col, None)
            col += 1
            sheet.write(row, col, vals['transaction_number']); col += 1
            sheet.write(row, col, vals['customer_name']); col += 1
            sheet.write(row, col, vals['currency_code']); col += 1
            sheet.write_number(row, col, vals['balance'], num_fmt); col += 1
            sheet.write_number(row, col, vals['amount'], num_fmt); col += 1
            sheet.write_number(row, col, vals['exchange_rate']); col += 1
            sheet.write_number(row, col, vals['aed_amount'], num_fmt); col += 1
            row += 1

        workbook.close()
        output.seek(0)

        attachment = self.env['ir.attachment'].create({
            'name': f"AR_Aging_Details_By_Invoice_Due_{self.as_of_date}.xlsx",
            'type': 'binary',
            'datas': base64.b64encode(output.read()),
            'res_model': self._name,
            'res_id': self.id,
        })

        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }