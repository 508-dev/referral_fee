# referral_fee

A custom ERPNext app for **508.dev** that automates referral fee Purchase Invoices.

When a Sales Invoice is submitted, the system automatically creates one Draft Purchase Invoice per referrer defined on the linked Project — no manual calculation or PI creation needed.

---

## For Admins: What This App Does

**Before this app:** Referral fees were created manually. Someone had to calculate the amount, find the right supplier, and create a Purchase Invoice by hand every time a Sales Invoice was submitted.

**After this app:** Set up referrers once on each Project. Every Sales Invoice submitted against that Project automatically generates the correct Draft Purchase Invoices.

```
Project: Cooper Dark Mater
  └── Referrers:
        ├── Michael Wu  — 10%
        └── Tony Sun    — 5%

Sales Invoice submitted (grand total $1,000)
  → Auto-creates Draft PI for Michael Wu: $100
  → Auto-creates Draft PI for Tony Sun:   $50
```

The Draft PIs can then be reviewed and submitted through the normal Purchase Invoice workflow.

---

## Business Rules

| Rule | Behaviour |
|---|---|
| Amount formula | `grand_total × percentage%` |
| Item used on PI | `Internal Commission` |
| PI status on creation | **Draft** — requires manual review before submitting |
| First-year limit | **Not enforced automatically.** Admins remove referrers from the Project after year 1 to stop PI creation |
| % total validation | Total referrer % on a Project cannot exceed 100% — blocked on save |
| Duplicate guard | Resubmitting the same Sales Invoice will not create duplicate PIs |
| Cancel behaviour | Cancelling a Sales Invoice auto-deletes its Draft referral PIs. Submitted PIs must be cancelled manually |

---

## For Engineers: Installation

### Standard install (production / any bench)

```bash
bench get-app https://github.com/508-dev/referral_fee
bench --site <sitename> install-app referral_fee
bench --site <sitename> migrate
```

`migrate` creates the `Project Referrer` child table and applies custom fields to Project and Purchase Invoice.

### Local Docker dev (508.dev setup)

The repo is mounted as a volume — no image rebuild needed.

1. Place the repo under `app/custom_apps/referral_fee/`
2. Add `referral_fee` to `app/dev-entrypoint.sh` (already there if cloning alongside `engineer_onboarding`)
3. Start containers:

```bash
cd app
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d
```

4. Install into the site (first time only):

```bash
# Add to apps.txt so ERPNext recognises the app
docker compose -f docker-compose.yml -f docker-compose.override.yml exec backend \
  bash -c "echo 'referral_fee' >> /home/frappe/frappe-bench/sites/apps.txt"

# Install app and apply schema
docker compose -f docker-compose.yml -f docker-compose.override.yml exec backend \
  bench --site frontend install-app referral_fee

docker compose -f docker-compose.yml -f docker-compose.override.yml exec backend \
  bench --site frontend migrate
```

5. After any Python changes, restart backend to reload:

```bash
docker compose -f docker-compose.yml -f docker-compose.override.yml restart backend
```

> **Note:** `docker-compose.override.yml` and `dev-entrypoint.sh` are Docker-level orchestration files — they are **not** part of this repo and should not be committed here.

---

## For Engineers: App Structure

```
referral_fee/
├── pyproject.toml                          # package metadata (flit)
├── README.md
└── referral_fee/                           # Frappe app package
    ├── hooks.py                            # event hooks & fixture config
    ├── install.py                          # after_install: creates "Referral Fee" service item
    ├── modules.txt
    ├── fixtures/
    │   └── custom_field.json               # custom fields added to Project & Purchase Invoice
    └── referral_fee/                       # module
        ├── referral_utils.py               # all business logic (on_submit, on_cancel, validate)
        └── doctype/
            └── project_referrer/           # child table: Supplier + Percentage per Project
                ├── project_referrer.json
                └── project_referrer.py
```

### Key file: `referral_utils.py`

| Function | Triggered by | Purpose |
|---|---|---|
| `on_sales_invoice_submit` | Sales Invoice submit | Creates Draft PIs for each referrer |
| `on_sales_invoice_cancel` | Sales Invoice cancel | Deletes Draft PIs linked to that SI |
| `validate_project_referrers` | Project save | Blocks save if total % > 100 |
| `_make_purchase_invoice` | internal | Builds and inserts one Purchase Invoice |
| `_get_expense_account` | internal | Finds expense account (tries "Referral Fee Expense", falls back to company default) |
| `_is_within_first_year` | unused (commented out) | Year-1 enforcement logic — ready to enable |

### Custom fields added (via fixtures)

**On Project:**
- `referrers` — Table (child table: `Project Referrer`) — the list of referrers and their %

**On Purchase Invoice:**
- `referral_source_si` — Link to Sales Invoice — which SI triggered this PI
- `is_referral_fee` — Check — marks auto-generated PIs (used by cancel logic to avoid touching manual PIs)

---

## How to Use (Step by Step)

### 1. Set up referrers on a Project

1. Open the Project
2. Click the **Referrers** tab
3. Add one row per referrer:
   - **Supplier** — must already exist as a Supplier in ERPNext
   - **Percentage (%)** — e.g. `10` for 10%
4. Save

> Total percentage across all referrers cannot exceed 100%.

### 2. Submit a Sales Invoice

1. Create a Sales Invoice as usual
2. Make sure the **Project** field is set to the project with referrers
3. Submit

The system will immediately create one Draft Purchase Invoice per referrer. A green notification with links to the new PIs will appear.

### 3. Review and submit the Draft PIs

Go to **Accounting > Purchase Invoice** and find the new Draft PIs. Review amounts, then submit them through the normal workflow.

---

## Testing Checklist

Run through these after any install or code change:

| Test | Steps | Expected result |
|---|---|---|
| **Normal flow** | Set 10% referrer on Project → Submit $1,000 SI | Draft PI created for $100 |
| **Multiple referrers** | Set 5% + 10% → Submit $1,000 SI | Two Draft PIs: $50 and $100 |
| **% validation** | Set referrers totalling > 100% → Save Project | Error: cannot exceed 100% |
| **Duplicate guard** | Submit same SI twice (or cancel + resubmit without amending) | Only one PI per referrer |
| **Cancel cleanup** | Submit SI → Cancel SI | Draft PI deleted automatically |
| **Amend flow** | Submit → Cancel → Amend → Resubmit | New PI created, no duplicates |
| **No project** | Submit SI with no Project set | No PI created, no error |
| **No referrers** | Submit SI against Project with empty Referrers tab | No PI created, no error |

---

## Enabling the First-Year Limit (Future)

The logic is written but commented out in `referral_utils.py` inside `on_sales_invoice_submit`. To enable automatic enforcement:

1. Uncomment the `if not _is_within_first_year(...)` block
2. Confirm that each Project has `Expected Start Date` set (the function uses this as the 1-year start point; falls back to the Project's creation date if not set)
3. Restart the backend

---

## Decisions Log

| Question | Decision | Confirmed by |
|---|---|---|
| Item on Purchase Invoice | `Internal Commission` | Caleb, 2026-05-04 |
| Rounding rule | Round to nearest penny using Python's standard `round(x, 2)` | Caleb, 2026-05-04 |
| First-year automatic enforcement | Deferred — handle manually for now. Code is written but commented out, ready to enable | Caleb, 2026-05-04 |
