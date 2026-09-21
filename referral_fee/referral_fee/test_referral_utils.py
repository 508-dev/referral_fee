from unittest import TestCase
from unittest.mock import patch

import frappe

from referral_fee.referral_fee import referral_utils


class TestReferralInvoiceLogging(TestCase):
    def setUp(self):
        self.sales_invoice = frappe._dict(
            {
                "name": "ACC-SINV-TEST-00001",
                "customer": "Test Customer",
                "project": "PROJ-TEST",
                "grand_total": 1000,
                "company": "Test Company",
                "currency": "USD",
            }
        )

    @patch.object(referral_utils.logger, "info")
    @patch.object(frappe, "get_doc")
    def test_logs_missing_project_skip_reason(self, get_doc, log_info):
        self.sales_invoice.project = None

        referral_utils.on_sales_invoice_submit(self.sales_invoice, "on_submit")

        get_doc.assert_not_called()
        entries = [call.args[0] for call in log_info.call_args_list]
        self.assertEqual(entries[-1]["event"], "referral_invoice.skipped")
        self.assertEqual(entries[-1]["reason"], "missing_project")

    @patch.object(referral_utils.logger, "info")
    @patch.object(frappe.db.after_commit, "add")
    @patch.object(referral_utils, "_make_purchase_invoice")
    @patch.object(frappe, "msgprint")
    @patch.object(frappe.db, "get_value", return_value=None)
    @patch.object(frappe, "get_doc")
    def test_logs_created_purchase_invoice(
        self,
        get_doc,
        _get_value,
        _msgprint,
        make_purchase_invoice,
        after_commit_add,
        log_info,
    ):
        get_doc.return_value = frappe._dict(
            {
                "referrers": [
                    frappe._dict(
                        {
                            "idx": 1,
                            "supplier": "Test Supplier",
                            "percentage": 10,
                        }
                    )
                ]
            }
        )
        make_purchase_invoice.return_value = frappe._dict(
            {"name": "ACC-PINV-TEST-00001"}
        )

        referral_utils.on_sales_invoice_submit(self.sales_invoice, "on_submit")

        make_purchase_invoice.assert_called_once_with(
            self.sales_invoice, "Test Supplier", 100.0
        )
        immediate_entries = [call.args[0] for call in log_info.call_args_list]
        self.assertNotIn(
            "referral_invoice.created",
            [entry["event"] for entry in immediate_entries],
        )

        self.assertEqual(after_commit_add.call_count, 2)
        for call in after_commit_add.call_args_list:
            call.args[0]()

        entries = [call.args[0] for call in log_info.call_args_list]
        created = next(
            entry for entry in entries if entry["event"] == "referral_invoice.created"
        )
        self.assertEqual(created["purchase_invoice"], "ACC-PINV-TEST-00001")
        self.assertEqual(created["amount"], 100.0)
        completed = entries[-1]
        self.assertEqual(completed["event"], "referral_invoice.processing_completed")
        self.assertEqual(completed["created_count"], 1)

    @patch.object(referral_utils.logger, "info")
    @patch.object(
        frappe.db.after_commit,
        "add",
        side_effect=lambda callback: callback(),
    )
    @patch.object(referral_utils, "_make_purchase_invoice")
    @patch.object(frappe.db, "get_value", return_value="ACC-PINV-TEST-00001")
    @patch.object(frappe, "get_doc")
    def test_logs_duplicate_skip_reason(
        self, get_doc, _get_value, make_purchase_invoice, _after_commit_add, log_info
    ):
        get_doc.return_value = frappe._dict(
            {
                "referrers": [
                    frappe._dict(
                        {
                            "idx": 1,
                            "supplier": "Test Supplier",
                            "percentage": 10,
                        }
                    )
                ]
            }
        )

        referral_utils.on_sales_invoice_submit(self.sales_invoice, "on_submit")

        make_purchase_invoice.assert_not_called()
        entries = [call.args[0] for call in log_info.call_args_list]
        skipped = next(
            entry
            for entry in entries
            if entry["event"] == "referral_invoice.referrer_skipped"
        )
        self.assertEqual(skipped["reason"], "duplicate_purchase_invoice")
        self.assertEqual(
            skipped["existing_purchase_invoice"], "ACC-PINV-TEST-00001"
        )

    @patch.object(referral_utils.logger, "exception")
    @patch.object(referral_utils.logger, "info")
    @patch.object(
        referral_utils,
        "_make_purchase_invoice",
        side_effect=RuntimeError("insert failed"),
    )
    @patch.object(frappe.db, "get_value", return_value=None)
    @patch.object(frappe, "get_doc")
    def test_logs_and_reraises_creation_failure(
        self,
        get_doc,
        _get_value,
        _make_purchase_invoice,
        _log_info,
        log_exception,
    ):
        get_doc.return_value = frappe._dict(
            {
                "referrers": [
                    frappe._dict(
                        {
                            "idx": 1,
                            "supplier": "Test Supplier",
                            "percentage": 10,
                        }
                    )
                ]
            }
        )

        with self.assertRaisesRegex(RuntimeError, "insert failed"):
            referral_utils.on_sales_invoice_submit(self.sales_invoice, "on_submit")

        entry = log_exception.call_args.args[0]
        self.assertEqual(entry["event"], "referral_invoice.creation_failed")
        self.assertEqual(entry["supplier"], "Test Supplier")
        self.assertEqual(entry["amount"], 100.0)
