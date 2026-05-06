import frappe


def after_install():
    _ensure_referral_fee_item()


def _ensure_referral_fee_item():
    if frappe.db.exists("Item", "Referral Fee"):
        return

    item_group = (
        "Services"
        if frappe.db.exists("Item Group", "Services")
        else "All Item Groups"
    )

    frappe.get_doc({
        "doctype": "Item",
        "item_code": "Referral Fee",
        "item_name": "Referral Fee",
        "item_group": item_group,
        "description": "Auto-generated line item for project referral fees.",
        "is_stock_item": 0,
        "is_purchase_item": 1,
        "is_sales_item": 0,
        "stock_uom": "Nos",
    }).insert(ignore_permissions=True)

    frappe.db.commit()
