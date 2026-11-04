import json
from unittest.mock import patch, Mock

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.ae_einvoice.models.einvoice_purchase_detail import populated_values, detail_rows


@tagged('post_install', '-at_install')
class TestPurchaseDetail(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendor = cls.env['res.partner'].create({'name': 'AP Test Supplier', 'vat': '100367656400003'})
        cls.tax = cls.env['account.tax'].create({
            'name': 'AP Test 5%', 'amount': 5, 'amount_type': 'percent',
            'type_tax_use': 'purchase', 'company_id': cls.env.company.id,
        })
        cls.env['einvoice.config'].create({})

    def _purchase(self):
        lookup = {
            'country': 'AE', 'entityTaxRegNo': '100443456700003', 'entityTin': '1004434567',
            'entityName': 'Buyer', 'supplyType': 'P', 'documentType': 'INV',
            'documentNumber': 'AP-TEST-1', 'documentDate': '2026-09-23 00:00:00',
            'billFromTaxRegNo': self.vendor.vat,
        }
        purchase = self.env['einvoice.purchase'].create({
            'company_id': self.env.company.id, 'entity_tax_reg_no': lookup['entityTaxRegNo'],
            'supplier_tax_reg_no': self.vendor.vat, 'document_type': 'INV',
            'document_number': lookup['documentNumber'], 'lookup_payload': json.dumps(lookup),
        })
        detail = dict(lookup, documentCurrency=self.env.company.currency_id.name,
                      paymentDueDate='2026-10-23', billFrom={
                          'taxRegNo': self.vendor.vat, 'legalName': 'techcarrot FZ-LLC',
                          'legalRegNo': '0235-100367656400003', 'endpoint': '0235:1003676564'},
                      billTo={'legalName': 'techcarrot Technologies LLC'},
                      invoiceTotalAmountWithoutTax=20000, invoiceTotalTaxAmount=1000,
                      totalDocumentAmount=21000, unused=None, exchangeRate=0,
                      documentItems=[{'name': 'Consulting', 'invoicedQuantity': 1,
                                      'netUnitPrice': 20000, 'priceBaseQuantity': 1, 'taxRate': 5}])
        response = Mock()
        response.json.return_value = [detail]
        with patch.object(type(self.env['einvoice.config']), '_get_access_token', return_value='test-token'), \
             patch('odoo.addons.ae_einvoice.models.einvoice_purchase_detail.requests.post', return_value=response) as post:
            purchase.action_view_details()
            self.assertEqual(post.call_args.kwargs['json']['criterias'][0], lookup)
        purchase.line_ids.write({
            'account_id': self.company_data['default_account_expense'].id,
            'tax_ids': [fields.Command.set(self.tax.ids)],
        })
        return purchase

    def test_nulls_and_empty_structures(self):
        cleaned = populated_values({'null': None, 'zero': 0, 'false': False,
                                    'nested': {'null': None}, 'list': [{'x': None}], 'yes': {'x': 1}})
        self.assertEqual(cleaned, {'zero': 0, 'false': False, 'yes': {'x': 1}})
        self.assertEqual(len(detail_rows(cleaned)), 3)

    def test_supplier_is_bill_from_not_buyer(self):
        purchase = self._purchase()
        self.assertEqual(purchase.partner_id, self.vendor)
        self.assertEqual(purchase.supplier_legal_name, 'techcarrot FZ-LLC')
        self.assertEqual(purchase.supplier_legal_reg_no, '0235-100367656400003')
        self.assertEqual(purchase.supplier_endpoint, '0235:1003676564')
        with self.assertRaises(ValidationError), self.cr.savepoint():
            purchase.partner_id = self.env.company.partner_id

    def test_fetch_keeps_edits_on_second_view(self):
        purchase = self._purchase()
        purchase.line_ids.name = 'Reviewed consulting'
        with patch('odoo.addons.ae_einvoice.models.einvoice_purchase_detail.requests.post') as post:
            purchase.action_view_details()
            post.assert_not_called()
        self.assertEqual(purchase.line_ids.name, 'Reviewed consulting')
        self.assertNotIn('unused', json.loads(purchase.detail_payload))

    def test_convert_hold_and_release(self):
        purchase = self._purchase()
        purchase.action_convert_to_bill()
        bill = purchase.bill_id
        self.assertEqual(purchase.state, 'matched')
        self.assertEqual(bill.state, 'draft')
        self.assertEqual(bill.amount_total, 21000)
        self.assertEqual(bill.einvoice_ap_state, 'matched')
        with self.assertRaises(UserError):
            bill.action_post()
        purchase.action_convert_to_bill()
        self.assertEqual(purchase.bill_id, bill)
        purchase.action_move_to_vendor_bills()
        self.assertEqual(bill.einvoice_ap_state, 'released')
        self.assertEqual(bill.state, 'draft')

    def test_mismatched_totals_rollback(self):
        purchase = self._purchase()
        purchase.expected_total = 999
        with self.assertRaises(UserError), self.cr.savepoint():
            purchase.action_convert_to_bill()
        self.assertFalse(purchase.bill_id)
        self.assertFalse(self.env['account.move'].search([('einvoice_ap_id', '=', purchase.id)]))

    def test_converted_line_is_locked(self):
        purchase = self._purchase()
        purchase.action_convert_to_bill()
        with self.assertRaises(UserError):
            purchase.line_ids.write({'price_unit': 1})
        with self.assertRaises(UserError):
            purchase.write({'state': 'released'})

    def test_refuse_review_locks_invoice_and_details(self):
        purchase = self._purchase()
        purchase.action_refuse()
        self.assertEqual(purchase.state, 'rejected')
        with self.assertRaises(UserError):
            purchase.write({'expected_total': 1})
        with self.assertRaises(UserError):
            purchase.line_ids.write({'price_unit': 1})
        with self.assertRaises(UserError):
            purchase.detail_value_ids[:1].write({'value': 'changed'})
        with self.assertRaises(UserError):
            purchase.action_convert_to_bill()
        with self.assertRaises(UserError):
            purchase.action_move_to_vendor_bills()

    def test_refuse_matched_cancels_and_locks_bill(self):
        purchase = self._purchase()
        purchase.action_convert_to_bill()
        purchase.action_refuse()
        self.assertEqual(purchase.state, 'rejected')
        self.assertEqual(purchase.bill_id.state, 'cancel')
        with self.assertRaises(UserError):
            purchase.bill_id.button_draft()
        with self.assertRaises(UserError):
            purchase.bill_id.invoice_line_ids.write({'price_unit': 1})

    def test_purchase_order_vendor_must_match(self):
        purchase = self._purchase()
        other = self.env['res.partner'].create({'name': 'Different Vendor'})
        order = self.env['purchase.order'].create({'partner_id': other.id, 'company_id': self.env.company.id})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            purchase.purchase_order_id = order
