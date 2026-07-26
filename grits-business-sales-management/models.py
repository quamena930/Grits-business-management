"""
Database models for the Grits Business Sales Management System.

Tables:
    User        - staff accounts that can log in, each with a role
                  (admin/manager/cashier/viewer) that drives permissions
    Setting     - single-row table of editable business settings
    Product     - the shop's catalogue: identity, pricing, and stock on hand
    Customer    - people/businesses the shop sells to
    Supplier    - vendors the shop buys stock from
    Sale        - one row per line item sold. Several Sale rows sharing the
                  same receipt_number make up one checkout/transaction -
                  see the note on Sale below for why it's modelled this way.
    ActivityLog - a running feed of notable actions (sales, stock changes,
                  logins, backups), used to power the dashboard's recent
                  activity panel and as a lightweight audit trail

'db' is created here (rather than in app.py) so that models.py has no
dependency on app.py, which avoids circular imports: app.py imports
from models.py, never the other way around.
"""

import uuid
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

# Shared choice list for how a sale was paid - used by forms.py (for the
# checkout form) and app.py/templates (to turn a stored value like
# "mobile_money" into the label "Mobile Money" on receipts and reports).
PAYMENT_METHODS = [
    ("cash", "Cash"),
    ("mobile_money", "Mobile Money"),
    ("card", "Card"),
    ("bank_transfer", "Bank Transfer"),
]
PAYMENT_METHOD_LABELS = dict(PAYMENT_METHODS)

# Four roles, matching a typical small-shop org chart. Permissions are
# enforced in app.py via the role_required() decorator - see the
# ROUTE PERMISSIONS note near the top of app.py for the full matrix.
ROLES = [
    ("admin", "Administrator"),
    ("manager", "Manager"),
    ("cashier", "Cashier"),
    ("viewer", "Viewer"),
]
ROLE_LABELS = dict(ROLES)


class User(UserMixin, db.Model):
    """A staff account. Flask-Login uses UserMixin for session handling
    (is_authenticated, get_id(), etc.). 'role' drives what the account is
    allowed to do - see role_required() in app.py."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="cashier")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, raw_password):
        """Hash and store a plaintext password. Never store passwords as-is."""
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        """Compare a plaintext password against the stored hash."""
        return check_password_hash(self.password_hash, raw_password)

    @property
    def role_label(self):
        return ROLE_LABELS.get(self.role, self.role.title())

    def __repr__(self):
        return f"<User {self.username} ({self.role})>"


class Setting(db.Model):
    """A single-row table holding the shop's editable business settings
    (Settings page). Kept as columns on one row rather than a generic
    key/value table since the set of settings is small and fixed - this
    stays simpler to read and to validate through a normal WTForm.
    Use get_settings() in app.py rather than querying this directly."""

    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    business_name = db.Column(db.String(120), nullable=False, default="Grits Business")
    business_address = db.Column(db.String(255), nullable=True)
    business_phone = db.Column(db.String(30), nullable=True)
    business_email = db.Column(db.String(120), nullable=True)
    currency_symbol = db.Column(db.String(10), nullable=False, default="GH\u20b5")
    default_tax_rate = db.Column(db.Float, nullable=False, default=0.0)
    low_stock_threshold_default = db.Column(db.Integer, nullable=False, default=5)
    receipt_footer = db.Column(db.String(255), nullable=False, default="Thank you for your business!")

    def __repr__(self):
        return f"<Setting business_name={self.business_name!r}>"


class Supplier(db.Model):
    """A vendor the shop buys stock from. Kept intentionally simple for
    now - contact details plus which products they supply. Purchase
    orders, supplier payments, and running balances are a natural next
    step once this is in use, but that's a bigger accounts-payable
    feature that deserves its own pass rather than being bolted on."""

    __tablename__ = "suppliers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    contact_person = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(30), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    products = db.relationship("Product", backref="supplier", lazy=True)

    def __repr__(self):
        return f"<Supplier {self.name}>"


class Customer(db.Model):
    """A person or business the shop sells to. Attaching a customer to a
    sale is optional - most small-shop transactions are walk-in/cash and
    don't need one."""

    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    city = db.Column(db.String(80), nullable=True)
    country = db.Column(db.String(80), nullable=True)
    customer_type = db.Column(db.String(20), nullable=False, default="retail")  # retail | wholesale
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def total_spent(self):
        return sum(s.total for s in self.sales)

    @property
    def purchase_count(self):
        """Number of distinct checkouts, not line items - a customer who
        bought 3 items in one visit made 1 purchase, not 3."""
        return len({s.receipt_number for s in self.sales})

    @property
    def last_purchase_at(self):
        if not self.sales:
            return None
        return max(s.date_created for s in self.sales)

    def __repr__(self):
        return f"<Customer {self.full_name}>"


class Product(db.Model):
    """An item the shop sells: identity (SKU/barcode/category/brand),
    pricing (cost vs selling price, tax), and stock on hand.

    cost_price and selling_price are kept separate (rather than a single
    'price') specifically so the dashboard can report real gross revenue
    vs net profit, not just turnover.
    """

    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)

    # Identity
    name = db.Column(db.String(120), unique=True, nullable=False)
    sku = db.Column(db.String(40), unique=True, nullable=False)
    barcode = db.Column(db.String(64), unique=True, nullable=True)
    category = db.Column(db.String(80), nullable=True)
    brand = db.Column(db.String(80), nullable=True)
    description = db.Column(db.Text, nullable=True)
    image_filename = db.Column(db.String(255), nullable=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=True)

    # Pricing
    cost_price = db.Column(db.Float, nullable=False, default=0.0)
    selling_price = db.Column(db.Float, nullable=False)
    tax_rate = db.Column(db.Float, nullable=False, default=0.0)  # percent, e.g. 12.5

    # Stock
    stock_quantity = db.Column(db.Integer, nullable=False, default=0)
    low_stock_threshold = db.Column(db.Integer, nullable=False, default=5)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sales = db.relationship("Sale", backref="product", lazy=True)

    @staticmethod
    def generate_sku(category=None):
        """A short, human-scannable SKU: category prefix + 6 random hex
        chars, e.g. BEV-4F2A9C. Falls back to GEN- if no category is set.
        Collisions are astronomically unlikely, but the caller still
        retries on IntegrityError just in case."""
        prefix = "".join(ch for ch in (category or "GEN") if ch.isalnum())[:3].upper() or "GEN"
        return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"

    @property
    def is_out_of_stock(self):
        return self.stock_quantity <= 0

    @property
    def is_low_stock(self):
        """Low but not empty - out-of-stock is tracked separately so the
        two alerts don't double-count the same product."""
        return 0 < self.stock_quantity <= self.low_stock_threshold

    @property
    def stock_status(self):
        if self.is_out_of_stock:
            return "out"
        if self.is_low_stock:
            return "low"
        return "ok"

    @property
    def stock_value(self):
        """Inventory valuation at cost - what the shop actually paid for
        what's currently on the shelf."""
        return self.cost_price * self.stock_quantity

    @property
    def margin_per_unit(self):
        return self.selling_price - self.cost_price

    def __repr__(self):
        return f"<Product {self.name}>"


class Sale(db.Model):
    """One line item sold. A checkout with several products in the cart
    produces several Sale rows that all share the same receipt_number -
    that shared number *is* the transaction/receipt, rather than
    introducing a separate parent table. This keeps every existing
    per-line query (revenue trends, top products, category breakdown)
    working unchanged, while still supporting real multi-item carts,
    receipts, and per-transaction customer/payment info.

    product_name/unit_price/unit_cost are snapshotted at the time of sale
    so historical records - and profit reporting - stay accurate even if
    the product is later renamed, repriced, or deleted. total is the
    final amount charged for this line (after its share of any discount,
    including its tax) - i.e. what the dashboard's revenue figures sum.
    """

    __tablename__ = "sales"

    id = db.Column(db.Integer, primary_key=True)
    receipt_number = db.Column(db.String(24), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    product_name = db.Column(db.String(120), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    unit_cost = db.Column(db.Float, nullable=False, default=0.0)
    line_discount = db.Column(db.Float, nullable=False, default=0.0)
    line_tax = db.Column(db.Float, nullable=False, default=0.0)
    total = db.Column(db.Float, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True)
    payment_method = db.Column(db.String(20), nullable=False, default="cash")
    sold_by = db.Column(db.String(80), nullable=True)
    date_created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    customer = db.relationship("Customer", backref="sales")

    @property
    def subtotal(self):
        """Before discount and tax - useful on the receipt line-by-line."""
        return self.unit_price * self.quantity

    @property
    def profit(self):
        """Tax collected isn't profit, so it's excluded here - only the
        margin above cost, net of this line's share of any discount."""
        return (self.unit_price - self.unit_cost) * self.quantity - self.line_discount

    @property
    def payment_method_label(self):
        return PAYMENT_METHOD_LABELS.get(self.payment_method, self.payment_method.title())

    def __repr__(self):
        return f"<Sale {self.product_name} x{self.quantity} ({self.receipt_number})>"


class ActivityLog(db.Model):
    """A lightweight, append-only feed of notable actions in the system.
    Powers the dashboard's 'Recent activity' panel today; the same table
    doubles as the starting point for a proper audit log later."""

    __tablename__ = "activity_logs"

    id = db.Column(db.Integer, primary_key=True)
    actor = db.Column(db.String(80), nullable=True)
    action = db.Column(db.String(50), nullable=False)
    description = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<ActivityLog {self.action}: {self.description}>"
