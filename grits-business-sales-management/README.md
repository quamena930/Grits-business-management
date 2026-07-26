# Grits Business Sales Management

A Flask web app for a small shop to manage its product catalogue, record
sales, track stock, and review reports — with login, an executive
dashboard, image-backed inventory, low-stock alerts, CSV export, and JSON
backups.

This started as a single-file "Grits Sales Recorder" script, then grew
into a modular app, then had its **Phase 1** (dashboard, fuller
inventory, activity log, dark mode) and **Phase 2** (shopping-cart
checkout, receipts, tax/discounts, customers/suppliers) upgrades, and
now its **Phase 3 upgrade**: role-based access control, an in-app
Settings page, user management, an activity log viewer, and an
automated test suite — while keeping every existing feature working.

## ⚠️ Upgrading from a previous version? Read this first

The database schema has changed across three upgrades now:

- **Phase 1:** `Product` gained new columns and `price` was renamed to
  `selling_price`; a new `activity_logs` table was added.
- **Phase 2:** `Sale` gained `receipt_number`, `customer_id`,
  `payment_method`, `line_discount`, and `line_tax`; new `customers` and
  `suppliers` tables were added; `Product` gained `supplier_id`.
- **Phase 3:** `User.is_admin` (boolean) became `User.role` (string:
  admin/manager/cashier/viewer); a new `settings` table was added.

SQLAlchemy's `db.create_all()` only creates tables that don't exist yet —
it will **not** alter an existing table. If you have an `instance/sales.db`
from before any of these upgrades:

```bash
rm instance/sales.db
python app.py   # recreates the schema fresh, and reseeds the admin user
```

This is a one-time reset. There's no real data to lose in a fresh
dev/coursework setup — if you have data you need to keep, export a CSV
from Reports and back up `instance/sales.db` before deleting it.
(Migrations via Flask-Migrate/Alembic are planned for a later phase so
future schema changes won't require this.)

## What's new in Phase 3

- **Roles** — every account is admin, manager, cashier, or viewer.
  Admins and managers can manage inventory/customers/suppliers and take
  backups; cashiers can additionally run the till (cart, checkout,
  receipts) but not touch inventory; viewers can look at everything but
  change nothing. The full matrix is on the Users page in the app.
- **Settings page** (admin only) — business name, address, phone,
  email, currency symbol, default tax rate, default low-stock level,
  and the receipt footer message are now editable in the app instead of
  only via `.env`. Changes apply immediately (see the caveat about
  multi-worker deployments in `_sync_config_from_settings()`'s
  docstring in `app.py`).
- **User management page** (admin only) — add, edit, or remove staff
  accounts and change roles. You can't delete your own account or leave
  the shop with zero administrators.
- **Activity log page** — the audit trail that's been quietly recording
  sales/stock/login/backup events since Phase 1 now has a real,
  paginated page to browse it, not just the dashboard's small feed.
- **A 403 page** for permission-denied, matching the 404/500 pages.
- **An automated test suite** (`tests/test_app.py`) covering login,
  the checkout discount/tax math, the full cart→checkout→receipt HTTP
  flow, role permissions, and customer stats. See Testing below.

## Project structure

```
grits-business-sales-management/
│
├── app.py                  # routes and application wiring
├── config.py                # settings (secret key, database path, uploads, ...)
├── models.py                 # SQLAlchemy models: User, Setting, Product, Customer, Supplier, Sale, ActivityLog
├── forms.py                   # Flask-WTF forms
├── requirements.txt
├── .env.example                # copy to .env for production config
├── README.md
│
├── templates/
│   ├── base.html                # shared sidebar layout, toasts, confirm modal
│   ├── login.html
│   ├── dashboard.html
│   ├── sales.html                # cart + checkout + recent receipts
│   ├── receipt.html               # printable receipt + PDF download link
│   ├── edit_sale.html              # edit one line item within a receipt
│   ├── products.html               # inventory list + search/filter
│   ├── product_form.html            # shared add/edit product form
│   ├── customers.html
│   ├── customer_form.html
│   ├── customer_detail.html          # profile + purchase history
│   ├── suppliers.html
│   ├── supplier_form.html
│   ├── users.html                     # admin only
│   ├── user_form.html                  # admin only
│   ├── settings.html                    # admin only
│   ├── activity_log.html
│   ├── reports.html
│   ├── 403.html
│   ├── 404.html
│   └── 500.html
│
├── static/
│   ├── css/style.css        # design tokens incl. dark-mode + receipt print styles
│   ├── js/script.js          # theme toggle, toasts, confirm modal, image preview
│   ├── images/products/       # uploaded product photos land here
│   └── icons/
│
├── tests/
│   └── test_app.py         # auth, checkout math, role permissions, customer stats
│
├── exports/                # CSV reports land here
├── backups/                 # JSON backups land here
└── instance/                 # sales.db lives here (Flask convention)
```

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`. The first run creates the database and a
default account:

- **Username:** `admin`
- **Password:** `admin123`

Change this password after your first login (Users page, since you're
logged in as an admin) before using the app with real data.

## How it works

- **Products** hold cost price, selling price, tax rate, and stock.
  Checking out a cart reduces stock automatically and snapshots each
  product's current name, selling price, cost price, and tax rate onto
  its sale line — so editing a product later never rewrites sales
  history or distorts past profit figures.
- **Sales are modelled as line items that share a receipt number.**
  There's no separate "transaction" table — a checkout with 3 products
  just creates 3 `Sale` rows with the same `receipt_number`. This kept
  every existing per-line report (top products, category breakdown,
  revenue trend) working unchanged while still supporting real carts
  and receipts. See the docstring on the `Sale` model for the reasoning.
- **Checkout** re-checks stock right before committing (not just when
  you added the item to the cart), applies each product's tax rate
  automatically, and splits any flat discount proportionally across the
  cart's items — the full split logic and its rounding rule are in
  `_build_checkout_lines()` in `app.py`.
- **Receipts** can be viewed, printed (browser print dialog, with a
  print-only stylesheet that hides the sidebar/nav), or downloaded as a
  PDF (generated with reportlab — no headless browser or system binary
  needed).
- **Editing a line item** within a receipt only changes its product and
  quantity; its share of the original discount stays as first allocated,
  and tax is recalculated from the (possibly new) product's tax rate.
  Editing the customer, payment method, or discount split *after*
  checkout isn't supported yet — see Roadmap.
- **Customers** are optional on a sale (most small-shop sales are
  walk-in). Deleting a customer keeps their past sales on record as
  walk-in sales rather than deleting sales history.
- **Suppliers** are linked to products via a simple `supplier_id`. There's
  no purchase-order or payment tracking yet — see Roadmap.
- **SKU** is auto-generated (category prefix + random code) if you
  leave it blank when adding a product; barcode is optional. Both must
  be unique when set.
- **Product images** are stored under `static/images/products/` with a
  randomised filename (never the original filename, for safety) and are
  capped at 2 MB.
- **Dashboard** shows current-period sales, all-time revenue/profit,
  inventory health, and three charts (30-day trend, top products,
  category breakdown). "Transactions" counts distinct receipts, not
  line items.
- **Reports** filters sales by date range and exports the filtered set
  as a CSV into `exports/`.
- **Back up data** (sidebar button) writes every product, customer,
  supplier, and sale to a timestamped JSON file in `backups/`. There's
  no restore flow yet.
- A product can't be deleted once it has sales recorded against it (to
  keep sales history intact); set its stock to 0 instead. A supplier
  can't be deleted while products are still linked to it.

## Testing

```bash
python -m unittest discover tests -v
```

Uses a temporary on-disk SQLite database created fresh for the test run
(not `sqlite:///:memory:` — an in-memory database is private to a single
connection, which trips up Flask-SQLAlchemy apps that open more than
one). No extra dependencies beyond what the app itself already needs.

Covered: login success/failure, the checkout discount/tax split (against
hand-computed numbers), a full add-to-cart → checkout → receipt HTTP
flow including stock deduction, role permission enforcement (who can and
can't add a product, access Settings, etc.), the last-admin/self-delete
guards on user management, and customer purchase stats. Not yet covered:
reports/exports, image uploads, PDF generation, suppliers.

## Notes on deploying

`app.run(debug=True)` is for local development only. For anything
public-facing: run behind a real WSGI server (e.g. `gunicorn app:app`),
set a strong `SECRET_KEY` and `DATABASE_URL` via environment variables
(see `.env.example`), and turn debug mode off.

## Roadmap

Phases 1, 2, and 3 are built. Later work (not yet built):

- **Rate limiting**, structured error logging, and database migrations
  (Flask-Migrate/Alembic) — schema changes still require deleting the
  dev database, as noted above.
- **Notifications** — low stock, daily/monthly sales summary, failed
  login attempts. The dashboard *shows* low stock and the activity log
  *records* logins, but nothing pushes a notification yet.
- **Universal search** across products/customers/suppliers/sales in one
  box (each section has its own search today).
- **Excel/PDF exports** for reports (CSV only today).
- **Two-factor auth**, session timeout, forgot-password/email
  verification — login is currently username + password only.
- **Supplier purchase orders, payments, and running balances** —
  suppliers are linked to products today but there's no accounts-payable
  tracking.
- **Full transaction-level editing** — changing the customer, payment
  method, or discount split on a receipt after checkout isn't supported;
  only a line item's product/quantity can be edited.
- **Automated/scheduled backups with restore** — backups are manual and
  one-way (no restore flow) today.
- Deployment files (`Procfile`, `render.yaml`) for one-click Render
  deployment, and broader test coverage (reports, exports, image
  uploads, suppliers).
