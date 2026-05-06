app_name = "referral_fee"
app_title = "Referral Fee"
app_publisher = "508.dev"
app_description = "Auto-creates referral Purchase Invoices when a Sales Invoice is submitted."
app_email = "admin@508.dev"
app_license = "mit"

after_install = "referral_fee.install.after_install"

# Export/import custom fields that belong to this module
fixtures = [
    {
        "dt": "Custom Field",
        "filters": [["module", "=", "Referral Fee"]],
    }
]

doc_events = {
    "Sales Invoice": {
        "on_submit": "referral_fee.referral_fee.referral_utils.on_sales_invoice_submit",
        "on_cancel": "referral_fee.referral_fee.referral_utils.on_sales_invoice_cancel",
    },
    "Project": {
        "validate": "referral_fee.referral_fee.referral_utils.validate_project_referrers",
    },
}
