import logging

import frappe
from frappe import _
from frappe.utils import add_days, add_years, flt, getdate, today

# Item used as the line item in auto-generated referral Purchase Invoices.
# "Internal Commission" is the existing item used for manual referral PIs in prod.
# Change this if Caleb decides to use a different item.
REFERRAL_FEE_ITEM = "Internal Commission"

# Keep a dedicated, site-level audit trail for referral invoice decisions. This is
# intentionally separate from the general web log so a missing invoice can be
# traced without reproducing the original Sales Invoice submission.
logger = frappe.logger("referral_fee", allow_site=True, file_count=20)
logger.setLevel(logging.INFO)


def _log_sales_invoice_event(level, event, doc, after_commit=False, **details):
    """Write a structured referral event with the source invoice context."""
    entry = {
        "event": event,
        "sales_invoice": doc.name,
        "customer": doc.customer,
        "project": doc.project,
        "grand_total": flt(doc.grand_total),
    }
    entry.update(details)

    def write_log():
        getattr(logger, level)(entry)

    if after_commit:
        frappe.db.after_commit.add(write_log)
    else:
        write_log()


def on_sales_invoice_submit(doc, method):
    """
    Triggered when a Sales Invoice is submitted.
    For each referrer defined on the linked Project, creates one Draft Purchase Invoice.
    Amount = grand_total × referrer_percentage%
    """
    _log_sales_invoice_event("info", "referral_invoice.processing_started", doc)

    if not doc.project:
        _log_sales_invoice_event(
            "info",
            "referral_invoice.skipped",
            doc,
            reason="missing_project",
        )
        return

    try:
        project = frappe.get_doc("Project", doc.project)
    except frappe.DoesNotExistError:
        _log_sales_invoice_event(
            "warning",
            "referral_invoice.skipped",
            doc,
            reason="project_not_found",
        )
        return

    referrers = project.get("referrers", [])
    if not referrers:
        _log_sales_invoice_event(
            "info",
            "referral_invoice.skipped",
            doc,
            reason="no_project_referrers",
        )
        return

    # ── First-year limit ──────────────────────────────────────────────────────
    # Per Caleb (2026-05-01): "Handle manually for now."
    # The Wiki says referral fees only cover the first year of project revenue,
    # but enforcing this automatically is deferred. Admins are expected to remove
    # referrers from the Project after year 1 to stop PI creation.
    #
    # To re-enable automatic enforcement, uncomment the block below:
    #
    # if not _is_within_first_year(project, doc.posting_date):
    #     frappe.msgprint(
    #         _(
    #             "Referral fees skipped: Sales Invoice date is beyond the first year of "
    #             "Project {0} (started {1})."
    #         ).format(
    #             doc.project,
    #             project.get("expected_start_date") or project.creation,
    #         ),
    #         alert=True,
    #         indicator="orange",
    #     )
    #     return
    # ─────────────────────────────────────────────────────────────────────────

    created_pis = []
    for row in referrers:
        supplier = row.supplier
        percentage = flt(row.percentage)
        row_index = row.get("idx")

        if not supplier:
            _log_sales_invoice_event(
                "warning",
                "referral_invoice.referrer_skipped",
                doc,
                reason="missing_supplier",
                referrer_row=row_index,
                percentage=percentage,
            )
            continue

        if not percentage:
            _log_sales_invoice_event(
                "info",
                "referral_invoice.referrer_skipped",
                doc,
                reason="zero_percentage",
                referrer_row=row_index,
                supplier=supplier,
            )
            continue

        # Guard: skip if a non-cancelled PI already exists for this SI + supplier.
        # Prevents duplicates if submit somehow fires more than once.
        existing = frappe.db.get_value(
            "Purchase Invoice",
            {
                "referral_source_si": doc.name,
                "supplier": supplier,
                "docstatus": ["!=", 2],
            },
            "name",
        )
        if existing:
            _log_sales_invoice_event(
                "info",
                "referral_invoice.referrer_skipped",
                doc,
                reason="duplicate_purchase_invoice",
                referrer_row=row_index,
                supplier=supplier,
                percentage=percentage,
                existing_purchase_invoice=existing,
            )
            continue

        # Formula confirmed with Caleb (2026-05-01): grand_total × %
        # Rounding confirmed with Caleb (2026-05-04): nearest penny, Python standard rounding.
        # Example: $304.91 × 10% = $30.491 → rounds to $30.49
        amount = round(flt(doc.grand_total) * percentage / 100, 2)
        if amount <= 0:
            _log_sales_invoice_event(
                "warning",
                "referral_invoice.referrer_skipped",
                doc,
                reason="non_positive_amount",
                referrer_row=row_index,
                supplier=supplier,
                percentage=percentage,
                amount=amount,
            )
            continue

        try:
            pi = _make_purchase_invoice(doc, supplier, amount)
        except Exception:
            _log_sales_invoice_event(
                "exception",
                "referral_invoice.creation_failed",
                doc,
                referrer_row=row_index,
                supplier=supplier,
                percentage=percentage,
                amount=amount,
            )
            raise

        created_pis.append(pi.name)
        _log_sales_invoice_event(
            "info",
            "referral_invoice.created",
            doc,
            after_commit=True,
            referrer_row=row_index,
            supplier=supplier,
            percentage=percentage,
            amount=amount,
            purchase_invoice=pi.name,
        )

    _log_sales_invoice_event(
        "info",
        "referral_invoice.processing_completed",
        doc,
        after_commit=True,
        created_count=len(created_pis),
        created_purchase_invoices=created_pis,
    )

    if created_pis:
        links = ", ".join(
            f'<a href="/app/purchase-invoice/{n}">{n}</a>' for n in created_pis
        )
        frappe.msgprint(
            _("Referral Purchase Invoice(s) created (Draft): {0}").format(links),
            title=_("Referral Fees Generated"),
            indicator="green",
        )


def on_sales_invoice_cancel(doc, method):
    """
    When a Sales Invoice is cancelled, clean up its referral Purchase Invoices.
    - Draft PIs (docstatus=0): deleted automatically (nothing has been paid yet).
    - Submitted PIs (docstatus=1): warn admin to cancel manually (accounting entries exist).
    """
    draft_pis = frappe.get_all(
        "Purchase Invoice",
        filters={"referral_source_si": doc.name, "is_referral_fee": 1, "docstatus": 0},
        pluck="name",
    )
    submitted_pis = frappe.get_all(
        "Purchase Invoice",
        filters={"referral_source_si": doc.name, "is_referral_fee": 1, "docstatus": 1},
        pluck="name",
    )

    _log_sales_invoice_event(
        "info",
        "referral_invoice.cancel_cleanup_started",
        doc,
        draft_purchase_invoices=draft_pis,
        submitted_purchase_invoices=submitted_pis,
    )

    for name in draft_pis:
        frappe.delete_doc("Purchase Invoice", name, ignore_permissions=True)

    _log_sales_invoice_event(
        "info",
        "referral_invoice.cancel_cleanup_completed",
        doc,
        after_commit=True,
        deleted_draft_purchase_invoices=draft_pis,
        submitted_purchase_invoices_requiring_manual_cancellation=submitted_pis,
    )

    if draft_pis:
        frappe.msgprint(
            _("Deleted {0} Draft referral Purchase Invoice(s).").format(len(draft_pis)),
            alert=True,
            indicator="orange",
        )

    if submitted_pis:
        links = ", ".join(
            f'<a href="/app/purchase-invoice/{n}">{n}</a>' for n in submitted_pis
        )
        frappe.msgprint(
            _(
                "Warning: {0} submitted referral Purchase Invoice(s) must be "
                "cancelled manually: {1}"
            ).format(len(submitted_pis), links),
            indicator="red",
        )


def validate_project_referrers(doc, method):
    """Prevent saving a Project whose referrer percentages exceed 100% in total."""
    referrers = doc.get("referrers", [])
    if not referrers:
        return
    # round to 9 decimal places to avoid float artifacts (e.g. 10.1 + 0.2 = 10.299999...)
    total = round(sum(flt(r.percentage) for r in referrers), 9)
    if total > 100:
        frappe.throw(
            _("Referrer percentages total {0}% — cannot exceed 100%.").format(
                frappe.bold(_format_percentage(total))
            )
        )


def _format_percentage(value):
    """Show precise percentages without trailing zero noise."""
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _is_within_first_year(project, invoice_date):
    """
    Return True if invoice_date is within 1 year of the project start.
    Uses expected_start_date; falls back to creation date if not set.
    Currently unused — see comment in on_sales_invoice_submit.
    """
    start = project.get("expected_start_date") or getdate(project.creation)
    if not start:
        return True
    cutoff = add_years(getdate(start), 1)
    return getdate(invoice_date) <= cutoff


def _make_purchase_invoice(sales_invoice, supplier, amount):
    """
    Create a Draft Purchase Invoice for one referrer.
    The PI is intentionally left as Draft so the admin can review before submitting.
    Two custom fields (referral_source_si, is_referral_fee) added via fixtures
    allow filtering all referral PIs in list view.
    """
    company = sales_invoice.company
    expense_account = _get_expense_account(company)

    # Accounting Dimensions: cost_center is required for GL entries in ERPNext.
    # Manual referral PIs use "Projects - {abbr}" cost center (observed in prod).
    # Try to find it first; fall back to the company default if not configured.
    cost_center = (
        frappe.db.get_value(
            "Cost Center",
            {"cost_center_name": "Projects", "company": company, "is_group": 0},
            "name",
        )
        or frappe.get_cached_value("Company", company, "cost_center")
    )
    posting_date = today()
    pi = frappe.get_doc({
        "doctype": "Purchase Invoice",
        "supplier": supplier,
        "posting_date": posting_date,
        "due_date": add_days(posting_date, 30),
        "company": company,
        "currency": sales_invoice.currency or "USD",
        # Accounting Dimensions — required for proper GL / project cost tracking.
        # "project" links the expense to the correct project in financial reports.
        # "cost_center" must be set at both header and item level for ERPNext to
        # post the journal entry correctly.
        "project": sales_invoice.project,
        "cost_center": cost_center,
        "referral_source_si": sales_invoice.name,
        # is_referral_fee = 1 marks this PI as auto-generated.
        # Used by on_cancel to identify which PIs to delete when the source SI
        # is cancelled, without touching manually created PIs (which stay at 0).
        "is_referral_fee": 1,
        "items": [
            {
                "item_code": REFERRAL_FEE_ITEM,
                "qty": 1,
                "rate": amount,
                "expense_account": expense_account,
                "project": sales_invoice.project,
                "cost_center": cost_center,
                "description": _(
                    "Referral fee — Sales Invoice {0} / Project {1}"
                ).format(sales_invoice.name, sales_invoice.project),
            }
        ],
        "remarks": _(
            "Auto-generated referral fee.\n"
            "Source Sales Invoice: {0}\n"
            "Project: {1}"
        ).format(sales_invoice.name, sales_invoice.project),
    })
    pi.flags.ignore_mandatory = True
    pi.insert(ignore_permissions=True)
    return pi


def _get_expense_account(company):
    """
    Look up expense account to use for referral PI line items.
    First tries an account explicitly named 'Referral Fee Expense' under this company.
    Falls back to the company's default expense account.
    """
    account = frappe.db.get_value(
        "Account",
        {"account_name": "Referral Fee Expense", "company": company, "is_group": 0},
        "name",
    )
    if account:
        return account
    return frappe.get_cached_value("Company", company, "default_expense_account")
