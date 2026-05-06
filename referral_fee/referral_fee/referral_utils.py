import frappe
from frappe import _
from frappe.utils import today, add_years, getdate, flt

# Item used as the line item in auto-generated referral Purchase Invoices.
# "Internal Commission" is the existing item used for manual referral PIs in prod.
# Change this if Caleb decides to use a different item.
REFERRAL_FEE_ITEM = "Internal Commission"


def on_sales_invoice_submit(doc, method):
    """
    Triggered when a Sales Invoice is submitted.
    For each referrer defined on the linked Project, creates one Draft Purchase Invoice.
    Amount = grand_total × referrer_percentage%
    """
    if not doc.project:
        return

    try:
        project = frappe.get_doc("Project", doc.project)
    except frappe.DoesNotExistError:
        return

    referrers = project.get("referrers", [])
    if not referrers:
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
        if not row.supplier or not flt(row.percentage):
            continue

        # Guard: skip if a non-cancelled PI already exists for this SI + supplier.
        # Prevents duplicates if submit somehow fires more than once.
        existing = frappe.db.get_value(
            "Purchase Invoice",
            {
                "referral_source_si": doc.name,
                "supplier": row.supplier,
                "docstatus": ["!=", 2],
            },
            "name",
        )
        if existing:
            continue

        # Formula confirmed with Caleb (2026-05-01): grand_total × %
        # Rounding confirmed with Caleb (2026-05-04): nearest penny, Python standard rounding.
        # Example: $304.91 × 10% = $30.491 → rounds to $30.49
        amount = round(flt(doc.grand_total) * flt(row.percentage) / 100, 2)
        if amount <= 0:
            continue

        pi = _make_purchase_invoice(doc, row.supplier, amount)
        created_pis.append(pi.name)

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

    for name in draft_pis:
        frappe.delete_doc("Purchase Invoice", name, ignore_permissions=True)

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
                frappe.bold(f"{total:g}")  # :g strips trailing zeros; no extra % here
            )
        )


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

    pi = frappe.get_doc({
        "doctype": "Purchase Invoice",
        "supplier": supplier,
        "posting_date": today(),
        "due_date": today(),
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
