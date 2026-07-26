"""
Grits Business Sales Management
================================
A Flask web application for a small shop to manage its product catalogue,
record sales through a real shopping-cart checkout, track stock, manage
customers and suppliers, control access by role, and review reports.

Modules:
    config.py   - fallback app settings (secret key, database location,
                  uploads, ...) read from environment variables; business
                  settings (name/address/tax/etc.) are then editable at
                  runtime via the in-app Settings page - see get_settings()
    models.py   - SQLAlchemy models: User, Setting, Product, Customer,
                  Supplier, Sale, ActivityLog
    forms.py    - Flask-WTF forms with server-side validation + CSRF
    app.py      - this file: routes and application wiring

Run it with:
    pip install -r requirements.txt
    python app.py

The first run creates the database and a default admin account
(username: admin / password: admin123) - change that password after
logging in for the first time.

NOTE ON UPGRADING FROM AN OLDER VERSION: the database schema has changed
across three upgrades now (Product gained new columns and 'price' became
'selling_price'; Sale gained receipt_number/customer_id/payment_method/
line_discount/line_tax; Customer, Supplier, and Setting tables are new;
User's is_admin boolean became a role string). SQLAlchemy's create_all()
only creates tables that don't exist yet - it will not alter an existing
table. If you have an instance/sales.db from before any of these
upgrades, delete it once and let the app recreate it fresh on next run.
See README.md.

ROUTE PERMISSIONS (enforced by the role_required() decorator below):
    Everyone logged in         : view dashboard, inventory, customers,
                                  suppliers, sales/receipts, reports,
                                  activity log (read-only)
    admin, manager, cashier    : cart/checkout, edit or delete a sale
    admin, manager             : add/edit/delete products, customers,
                                  suppliers; create a backup
    admin only                 : Settings page, user management
"""

import csv
import json
import os
import uuid
from datetime import datetime, date, timedelta
from functools import wraps
from io import BytesIO

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_from_directory,
    send_file,
    session,
    abort,
)
from flask_login import (
    LoginManager,
    login_user,
    logout_user,
    login_required,
    current_user,
)

from config import Config
from models import db, User, Setting, Product, Customer, Supplier, Sale, ActivityLog, ROLES
from forms import (
    LoginForm,
    ProductForm,
    ProductSearchForm,
    CartAddForm,
    CheckoutForm,
    SaleForm,
    CustomerForm,
    SupplierForm,
    ReportFilterForm,
    UserForm,
    SettingsForm,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to continue."
login_manager.login_message_category = "error"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def role_required(*roles):
    """Restricts a view to the given roles. Always stack this *under*
    @login_required (login_required goes on top), so an anonymous visitor
    gets redirected to the login page rather than a 403:

        @app.route(...)
        @login_required
        @role_required("admin", "manager")
        def some_view(): ...
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return view_func(*args, **kwargs)

        return wrapped

    return decorator


# Config keys mirrored onto the Setting table so they're editable from
# the in-app Settings page instead of only via .env. SECRET_KEY and
# DATABASE_URL deliberately stay environment-only - those are deployment
# concerns, not shop settings.
SETTINGS_FIELDS = [
    "business_name",
    "business_address",
    "business_phone",
    "business_email",
    "currency_symbol",
    "default_tax_rate",
    "low_stock_threshold_default",
    "receipt_footer",
]


def get_settings():
    """Returns the single Setting row, creating it from Config's
    environment-derived defaults on first run."""
    settings = db.session.get(Setting, 1)
    if settings is None:
        settings = Setting(
            id=1,
            business_name=app.config["BUSINESS_NAME"],
            business_address=app.config["BUSINESS_ADDRESS"],
            business_phone=app.config["BUSINESS_PHONE"],
            business_email=app.config["BUSINESS_EMAIL"],
            currency_symbol=app.config["CURRENCY_SYMBOL"],
            default_tax_rate=app.config["DEFAULT_TAX_RATE"],
            low_stock_threshold_default=app.config["LOW_STOCK_THRESHOLD_DEFAULT"],
        )
        db.session.add(settings)
        db.session.commit()
    return settings


def _sync_config_from_settings(settings):
    """Copies the Setting row onto app.config, so every place that
    already reads app.config['BUSINESS_NAME'] etc. (templates included -
    Flask exposes `config` as a Jinja global) picks up saved changes
    immediately, without a restart.

    Known limitation: app.config lives in process memory, so under a
    multi-worker production server (e.g. multiple gunicorn workers) a
    settings change only takes effect for the worker that saved it until
    the others restart. Fine for the single-process dev server this
    project ships with; worth revisiting (read Setting per-request
    instead of caching) before a multi-worker deployment."""
    app.config["BUSINESS_NAME"] = settings.business_name
    app.config["BUSINESS_ADDRESS"] = settings.business_address or ""
    app.config["BUSINESS_PHONE"] = settings.business_phone or ""
    app.config["BUSINESS_EMAIL"] = settings.business_email or ""
    app.config["CURRENCY_SYMBOL"] = settings.currency_symbol
    app.config["DEFAULT_TAX_RATE"] = settings.default_tax_rate
    app.config["LOW_STOCK_THRESHOLD_DEFAULT"] = settings.low_stock_threshold_default
    app.config["RECEIPT_FOOTER"] = settings.receipt_footer


@app.context_processor
def inject_globals():
    """Values every template should have access to, without passing them
    explicitly from each view function."""
    return {"today_display": datetime.now().strftime("%A, %d %B %Y")}


def init_db():
    """Create tables on first run, seed a default admin account and the
    settings row, and load saved settings onto app.config."""
    db.create_all()
    if User.query.count() == 0:
        admin = User(username="admin", role="admin")
        admin.set_password("admin123")
        db.session.add(admin)
        db.session.commit()
        print("Created default admin account -> username: admin / password: admin123")
        print("Please change this password after your first login.")

    _sync_config_from_settings(get_settings())


# ---------------------------------------------------------------------------
# Small helpers shared across routes
# ---------------------------------------------------------------------------

def log_activity(action, description):
    """Append an entry to the activity feed. Doesn't commit - the caller
    folds this into whatever commit() it's already about to do, so one
    user action = one atomic database transaction."""
    entry = ActivityLog(
        actor=current_user.username if current_user.is_authenticated else "system",
        action=action,
        description=description,
    )
    db.session.add(entry)


def _allowed_image(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in app.config["ALLOWED_IMAGE_EXTENSIONS"]


def _save_product_image(file_storage):
    """Save an uploaded product image under a random filename (never trust
    the client's original filename) and return that filename, or None if
    no valid file was provided."""
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None
    if not _allowed_image(file_storage.filename):
        flash("Image skipped: only jpg, png, webp, or gif files are supported.", "error")
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(app.config["PRODUCT_IMAGES_DIR"], filename))
    return filename


def _delete_product_image(filename):
    if not filename:
        return
    path = os.path.join(app.config["PRODUCT_IMAGES_DIR"], filename)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _unique_sku(category):
    """Product.generate_sku() is already collision-resistant (random hex),
    but we still check, since 'name already exists' style bugs are the
    worst kind to debug in production."""
    for _ in range(5):
        candidate = Product.generate_sku(category)
        if not Product.query.filter_by(sku=candidate).first():
            return candidate
    return Product.generate_sku(category) + uuid.uuid4().hex[:2]


def _distinct_categories():
    rows = (
        db.session.query(Product.category)
        .filter(Product.category.isnot(None))
        .distinct()
        .order_by(Product.category)
        .all()
    )
    return [r[0] for r in rows if r[0]]


def _supplier_choices():
    return [("", "No supplier")] + [
        (str(s.id), s.name) for s in Supplier.query.order_by(Supplier.name).all()
    ]


def _customer_choices():
    return [("0", "Walk-in / no customer on file")] + [
        (str(c.id), c.full_name) for c in Customer.query.order_by(Customer.full_name).all()
    ]


def _group_sales_into_receipts(lines):
    """Turn a flat list of Sale rows (line items) into one summary dict
    per receipt_number, most recent first. Grouping happens in Python
    rather than SQL because a receipt's customer/payment/date are the
    same on every one of its lines, and picking 'one representative row
    per group' isn't something GROUP BY can express portably."""
    grouped = {}
    for line in lines:
        grouped.setdefault(line.receipt_number, []).append(line)

    receipts = []
    for receipt_number, group_lines in grouped.items():
        receipts.append(
            {
                "receipt_number": receipt_number,
                "date_created": group_lines[0].date_created,
                "item_count": len(group_lines),
                "grand_total": sum(l.total for l in group_lines),
                "payment_method_label": group_lines[0].payment_method_label,
                "customer": group_lines[0].customer,
            }
        )
    receipts.sort(key=lambda r: r["date_created"], reverse=True)
    return receipts


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data.strip()).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            log_activity("login", f"{user.username} logged in")
            db.session.commit()
            flash(f"Welcome back, {user.username}.", "success")
            next_page = request.args.get("next")
            return redirect(next_page or url_for("dashboard"))
        flash("Incorrect username or password.", "error")

    return render_template("login.html", form=form)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _sales_query(start=None, end=None):
    query = Sale.query
    if start:
        query = query.filter(db.func.date(Sale.date_created) >= start.isoformat())
    if end:
        query = query.filter(db.func.date(Sale.date_created) <= end.isoformat())
    return query


def _revenue(start=None, end=None):
    return (
        _sales_query(start, end)
        .with_entities(db.func.coalesce(db.func.sum(Sale.total), 0.0))
        .scalar()
    )


def _profit(start=None, end=None):
    return (
        _sales_query(start, end)
        .with_entities(
            db.func.coalesce(
                db.func.sum((Sale.unit_price - Sale.unit_cost) * Sale.quantity - Sale.line_discount), 0.0
            )
        )
        .scalar()
    )


def _units_sold(start=None, end=None):
    return (
        _sales_query(start, end)
        .with_entities(db.func.coalesce(db.func.sum(Sale.quantity), 0))
        .scalar()
    )


@app.route("/dashboard")
@login_required
def dashboard():
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)

    todays_sales = _revenue(today, today)
    weekly_sales = _revenue(week_start, today)
    monthly_sales = _revenue(month_start, today)
    annual_sales = _revenue(year_start, today)
    gross_revenue = _revenue()
    net_profit = _profit()
    transaction_count = (
        db.session.query(db.func.count(db.func.distinct(Sale.receipt_number))).scalar() or 0
    )
    products_sold = _units_sold() or 0

    inventory_value = (
        db.session.query(db.func.coalesce(db.func.sum(Product.cost_price * Product.stock_quantity), 0.0))
        .scalar()
    )

    low_stock_products = (
        Product.query.filter(Product.stock_quantity > 0, Product.stock_quantity <= Product.low_stock_threshold)
        .order_by(Product.stock_quantity)
        .all()
    )
    out_of_stock_products = (
        Product.query.filter(Product.stock_quantity <= 0).order_by(Product.name).all()
    )

    top_products = (
        db.session.query(
            Sale.product_name,
            db.func.sum(Sale.quantity).label("qty"),
            db.func.sum(Sale.total).label("revenue"),
        )
        .group_by(Sale.product_name)
        .order_by(db.desc("qty"))
        .limit(10)
        .all()
    )

    recent_sales = Sale.query.order_by(Sale.date_created.desc()).limit(8).all()
    recent_activity = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(10).all()

    # 30-day revenue trend for the area chart.
    trend_labels, trend_values = [], []
    for offset in range(29, -1, -1):
        day = today - timedelta(days=offset)
        trend_labels.append(day.strftime("%d %b"))
        trend_values.append(round(_revenue(day, day), 2))

    # Revenue by category (all-time) for the breakdown chart.
    category_rows = (
        db.session.query(Product.category, db.func.coalesce(db.func.sum(Sale.total), 0.0))
        .join(Sale, Sale.product_id == Product.id)
        .group_by(Product.category)
        .all()
    )
    category_labels = [c or "Uncategorised" for c, _ in category_rows]
    category_values = [round(v, 2) for _, v in category_rows]

    return render_template(
        "dashboard.html",
        todays_sales=todays_sales,
        weekly_sales=weekly_sales,
        monthly_sales=monthly_sales,
        annual_sales=annual_sales,
        gross_revenue=gross_revenue,
        net_profit=net_profit,
        transaction_count=transaction_count,
        products_sold=products_sold,
        inventory_value=inventory_value,
        low_stock_products=low_stock_products,
        out_of_stock_products=out_of_stock_products,
        top_products=top_products,
        recent_sales=recent_sales,
        recent_activity=recent_activity,
        trend_labels=trend_labels,
        trend_values=trend_values,
        category_labels=category_labels,
        category_values=category_values,
        product_count=Product.query.count(),
        customer_count=Customer.query.count(),
    )


# ---------------------------------------------------------------------------
# Sales: cart + checkout + receipts
# ---------------------------------------------------------------------------

def _product_choices():
    currency = app.config["CURRENCY_SYMBOL"]
    return [
        (p.id, f"{p.name} ({p.sku}) \u2014 {currency}{p.selling_price:.2f} ({p.stock_quantity} in stock)")
        for p in Product.query.order_by(Product.name).all()
    ]


def _build_checkout_lines(cart_lines, discount_amount):
    """cart_lines: list of (Product, quantity) tuples. Splits a single
    flat discount_amount across lines proportionally to each line's
    share of the pre-discount subtotal, then applies each product's own
    tax_rate to the post-discount amount. The last line absorbs any
    leftover fraction of a cent from rounding, so the lines always sum
    exactly to the intended total."""
    subtotal_total = sum(product.selling_price * qty for product, qty in cart_lines)
    built = []
    discount_allocated = 0.0

    for index, (product, qty) in enumerate(cart_lines):
        line_subtotal = product.selling_price * qty
        is_last = index == len(cart_lines) - 1

        if is_last:
            line_discount = round(discount_amount - discount_allocated, 2)
        elif subtotal_total > 0:
            share = line_subtotal / subtotal_total
            line_discount = round(discount_amount * share, 2)
        else:
            line_discount = 0.0
        discount_allocated += line_discount

        taxable_amount = max(line_subtotal - line_discount, 0.0)
        line_tax = round(taxable_amount * (product.tax_rate / 100.0), 2)
        line_total = round(line_subtotal - line_discount + line_tax, 2)

        built.append(
            {
                "product": product,
                "quantity": qty,
                "unit_price": product.selling_price,
                "unit_cost": product.cost_price,
                "line_discount": line_discount,
                "line_tax": line_tax,
                "total": line_total,
            }
        )
    return built


@app.route("/sales")
@login_required
def sales():
    cart = session.get("cart", [])
    cart_lines = []
    cart_was_pruned = False
    for item in cart:
        product = db.session.get(Product, item["product_id"])
        if product is None:
            cart_was_pruned = True
            continue
        cart_lines.append(
            {"product": product, "quantity": item["quantity"], "subtotal": product.selling_price * item["quantity"]}
        )

    if cart_was_pruned:
        session["cart"] = [{"product_id": l["product"].id, "quantity": l["quantity"]} for l in cart_lines]
        session.modified = True
        flash("One or more items in your cart are no longer available and were removed.", "error")

    cart_subtotal = sum(l["subtotal"] for l in cart_lines)

    add_form = CartAddForm()
    add_form.product_id.choices = _product_choices()
    if not add_form.product_id.choices:
        flash("Add at least one product before recording a sale.", "error")

    checkout_form = CheckoutForm()
    checkout_form.customer_id.choices = _customer_choices()

    recent_lines = Sale.query.order_by(Sale.date_created.desc()).limit(300).all()
    receipts = _group_sales_into_receipts(recent_lines)[:50]

    return render_template(
        "sales.html",
        cart_lines=cart_lines,
        cart_subtotal=cart_subtotal,
        add_form=add_form,
        checkout_form=checkout_form,
        receipts=receipts,
    )


@app.route("/sales/cart/add", methods=["POST"])
@login_required
@role_required("admin", "manager", "cashier")
def cart_add():
    add_form = CartAddForm()
    add_form.product_id.choices = _product_choices()
    if not add_form.validate_on_submit():
        flash("Choose a product and a valid quantity.", "error")
        return redirect(url_for("sales"))

    product = db.session.get(Product, add_form.product_id.data)
    if product is None:
        flash("That product no longer exists.", "error")
        return redirect(url_for("sales"))

    cart = session.get("cart", [])
    existing_item = next((item for item in cart if item["product_id"] == product.id), None)
    new_quantity = add_form.quantity.data + (existing_item["quantity"] if existing_item else 0)

    if new_quantity > product.stock_quantity:
        flash(f"Only {product.stock_quantity} unit(s) of {product.name} in stock.", "error")
        return redirect(url_for("sales"))

    if existing_item:
        existing_item["quantity"] = new_quantity
    else:
        cart.append({"product_id": product.id, "quantity": new_quantity})

    session["cart"] = cart
    session.modified = True
    flash(f"Added {add_form.quantity.data} x {product.name} to the cart.", "success")
    return redirect(url_for("sales"))


@app.route("/sales/cart/remove/<int:index>", methods=["POST"])
@login_required
@role_required("admin", "manager", "cashier")
def cart_remove(index):
    cart = session.get("cart", [])
    if 0 <= index < len(cart):
        cart.pop(index)
        session["cart"] = cart
        session.modified = True
    return redirect(url_for("sales"))


@app.route("/sales/cart/clear", methods=["POST"])
@login_required
@role_required("admin", "manager", "cashier")
def cart_clear():
    session["cart"] = []
    session.modified = True
    flash("Cart cleared.", "success")
    return redirect(url_for("sales"))


@app.route("/sales/checkout", methods=["POST"])
@login_required
@role_required("admin", "manager", "cashier")
def checkout():
    cart = session.get("cart", [])
    if not cart:
        flash("Your cart is empty.", "error")
        return redirect(url_for("sales"))

    checkout_form = CheckoutForm()
    checkout_form.customer_id.choices = _customer_choices()

    if not checkout_form.validate_on_submit():
        flash("Please correct the checkout details and try again.", "error")
        return redirect(url_for("sales"))

    # Re-validate stock right before committing - it can change between
    # "add to cart" and "checkout", especially with more than one till.
    cart_lines = []
    for item in cart:
        product = db.session.get(Product, item["product_id"])
        if product is None:
            continue
        qty = item["quantity"]
        if qty > product.stock_quantity:
            flash(
                f"Only {product.stock_quantity} unit(s) of {product.name} available now \u2014 update your cart.",
                "error",
            )
            return redirect(url_for("sales"))
        cart_lines.append((product, qty))

    if not cart_lines:
        flash("Your cart is empty.", "error")
        return redirect(url_for("sales"))

    raw_customer_id = checkout_form.customer_id.data
    customer_id = int(raw_customer_id) if raw_customer_id and raw_customer_id != "0" else None
    receipt_number = f"RCT-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

    built_lines = _build_checkout_lines(cart_lines, checkout_form.discount_amount.data or 0.0)
    for line in built_lines:
        product = line["product"]
        sale = Sale(
            receipt_number=receipt_number,
            product_id=product.id,
            product_name=product.name,
            quantity=line["quantity"],
            unit_price=line["unit_price"],
            unit_cost=line["unit_cost"],
            line_discount=line["line_discount"],
            line_tax=line["line_tax"],
            total=line["total"],
            customer_id=customer_id,
            payment_method=checkout_form.payment_method.data,
            sold_by=current_user.username,
        )
        product.stock_quantity -= line["quantity"]
        db.session.add(sale)

    grand_total = sum(line["total"] for line in built_lines)
    log_activity(
        "sale_created",
        f"Checkout {receipt_number}: {len(built_lines)} item(s) for {app.config['CURRENCY_SYMBOL']}{grand_total:.2f}",
    )
    db.session.commit()

    session["cart"] = []
    session.modified = True
    flash(f"Sale completed \u2014 receipt {receipt_number}.", "success")
    return redirect(url_for("view_receipt", receipt_number=receipt_number))


@app.route("/sales/receipt/<receipt_number>")
@login_required
def view_receipt(receipt_number):
    lines = Sale.query.filter_by(receipt_number=receipt_number).order_by(Sale.id).all()
    if not lines:
        abort(404)

    return render_template(
        "receipt.html",
        lines=lines,
        receipt_number=receipt_number,
        subtotal=sum(l.subtotal for l in lines),
        total_discount=sum(l.line_discount for l in lines),
        total_tax=sum(l.line_tax for l in lines),
        grand_total=sum(l.total for l in lines),
        customer=lines[0].customer,
        payment_method_label=lines[0].payment_method_label,
        cashier=lines[0].sold_by,
        date_created=lines[0].date_created,
    )


@app.route("/sales/receipt/<receipt_number>/pdf")
@login_required
def receipt_pdf(receipt_number):
    lines = Sale.query.filter_by(receipt_number=receipt_number).order_by(Sale.id).all()
    if not lines:
        abort(404)

    buffer = _render_receipt_pdf(lines, receipt_number)
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"receipt-{receipt_number}.pdf",
    )


def _render_receipt_pdf(lines, receipt_number):
    """Draws a narrow, thermal-receipt-style PDF using reportlab's plain
    canvas API (no HTML-to-PDF conversion, so no extra system
    dependencies like a headless browser or wkhtmltopdf binary)."""
    from reportlab.pdfgen import canvas as pdf_canvas
    from reportlab.lib.units import mm

    currency = app.config["CURRENCY_SYMBOL"]
    width = 80 * mm
    line_height = 14
    # Generous estimate: each sale line can draw up to 4 rows (name,
    # qty/price, discount, tax). Extra blank space just means a little
    # trailing whitespace, never clipped content.
    height = 40 + (12 + len(lines) * 4 + 10) * line_height

    buffer = BytesIO()
    c = pdf_canvas.Canvas(buffer, pagesize=(width, height))
    y = height - 24

    def draw_center(text, size=11, font="Helvetica-Bold"):
        nonlocal y
        c.setFont(font, size)
        c.drawCentredString(width / 2, y, text)
        y -= line_height

    def draw_left(text, size=8, font="Helvetica"):
        nonlocal y
        c.setFont(font, size)
        c.drawString(6, y, text)
        y -= line_height

    def draw_row(left, right, size=8, font="Helvetica"):
        nonlocal y
        c.setFont(font, size)
        c.drawString(6, y, left)
        c.drawRightString(width - 6, y, right)
        y -= line_height

    def draw_divider():
        nonlocal y
        c.line(6, y, width - 6, y)
        y -= line_height * 0.6

    draw_center(app.config["BUSINESS_NAME"], size=12)
    if app.config["BUSINESS_ADDRESS"]:
        draw_center(app.config["BUSINESS_ADDRESS"], size=7, font="Helvetica")
    if app.config["BUSINESS_PHONE"]:
        draw_center(app.config["BUSINESS_PHONE"], size=7, font="Helvetica")

    draw_divider()
    draw_left(f"Receipt: {receipt_number}")
    draw_left(f"Date: {lines[0].date_created.strftime('%d %b %Y, %H:%M')}")
    draw_left(f"Cashier: {lines[0].sold_by or '-'}")
    if lines[0].customer:
        draw_left(f"Customer: {lines[0].customer.full_name}")
    draw_left(f"Payment: {lines[0].payment_method_label}")
    draw_divider()

    for line in lines:
        draw_left(line.product_name, size=8, font="Helvetica-Bold")
        draw_row(f"  {line.quantity} x {currency}{line.unit_price:.2f}", f"{currency}{line.total:.2f}")
        if line.line_discount:
            draw_row("  Discount", f"-{currency}{line.line_discount:.2f}", size=7)
        if line.line_tax:
            draw_row("  Tax", f"{currency}{line.line_tax:.2f}", size=7)

    draw_divider()
    subtotal = sum(l.subtotal for l in lines)
    total_discount = sum(l.line_discount for l in lines)
    total_tax = sum(l.line_tax for l in lines)
    grand_total = sum(l.total for l in lines)

    draw_row("Subtotal", f"{currency}{subtotal:.2f}")
    if total_discount:
        draw_row("Discount", f"-{currency}{total_discount:.2f}")
    if total_tax:
        draw_row("Tax", f"{currency}{total_tax:.2f}")
    draw_row("TOTAL", f"{currency}{grand_total:.2f}", size=11, font="Helvetica-Bold")

    y -= 6
    draw_center(app.config["RECEIPT_FOOTER"], size=8, font="Helvetica-Oblique")

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


@app.route("/sales/receipt/<receipt_number>/delete", methods=["POST"])
@login_required
@role_required("admin", "manager", "cashier")
def delete_receipt(receipt_number):
    lines = Sale.query.filter_by(receipt_number=receipt_number).all()
    if not lines:
        abort(404)

    for line in lines:
        product = db.session.get(Product, line.product_id)
        if product is not None:
            product.stock_quantity += line.quantity
        db.session.delete(line)

    log_activity("sale_deleted", f"Deleted entire receipt {receipt_number} ({len(lines)} item(s))")
    db.session.commit()
    flash(f"Receipt {receipt_number} deleted and stock restored.", "success")
    return redirect(url_for("sales"))


@app.route("/sales/edit/<int:sale_id>", methods=["GET", "POST"])
@login_required
@role_required("admin", "manager", "cashier")
def edit_sale(sale_id):
    """Edits one line item within an existing receipt (product/quantity
    only). Discount stays as originally allocated at checkout; tax is
    recalculated from the (possibly new) product's tax rate."""
    sale = db.session.get(Sale, sale_id)
    if sale is None:
        abort(404)

    form = SaleForm()
    form.product_id.choices = _product_choices()
    if request.method == "GET":
        form.product_id.data = sale.product_id
        form.quantity.data = sale.quantity

    if form.validate_on_submit():
        new_product = db.session.get(Product, form.product_id.data)
        if new_product is None:
            flash("That product no longer exists.", "error")
            return redirect(url_for("edit_sale", sale_id=sale.id))

        # Put the sale's original quantity back into stock first, so
        # re-checking availability below isn't skewed by its own old sale.
        old_product = db.session.get(Product, sale.product_id)
        if old_product is not None:
            old_product.stock_quantity += sale.quantity

        if form.quantity.data > new_product.stock_quantity:
            if old_product is not None:
                old_product.stock_quantity -= sale.quantity  # roll back
            flash(f"Only {new_product.stock_quantity} unit(s) of {new_product.name} available.", "error")
            return redirect(url_for("edit_sale", sale_id=sale.id))

        new_product.stock_quantity -= form.quantity.data

        new_subtotal = form.quantity.data * new_product.selling_price
        taxable_amount = max(new_subtotal - sale.line_discount, 0.0)
        new_tax = round(taxable_amount * (new_product.tax_rate / 100.0), 2)

        sale.product_id = new_product.id
        sale.product_name = new_product.name
        sale.quantity = form.quantity.data
        sale.unit_price = new_product.selling_price
        sale.unit_cost = new_product.cost_price
        sale.line_tax = new_tax
        sale.total = round(new_subtotal - sale.line_discount + new_tax, 2)

        log_activity(
            "sale_edited",
            f"Edited line item in receipt {sale.receipt_number} \u2192 {sale.quantity} x {sale.product_name}",
        )
        db.session.commit()
        flash("Line item updated successfully.", "success")
        return redirect(url_for("view_receipt", receipt_number=sale.receipt_number))

    return render_template("edit_sale.html", form=form, sale=sale)


@app.route("/sales/delete/<int:sale_id>", methods=["POST"])
@login_required
@role_required("admin", "manager", "cashier")
def delete_sale(sale_id):
    sale = db.session.get(Sale, sale_id)
    if sale is None:
        abort(404)

    product = db.session.get(Product, sale.product_id)
    if product is not None:
        product.stock_quantity += sale.quantity  # restock on delete

    receipt_number = sale.receipt_number
    description = f"Deleted line item from receipt {receipt_number} ({sale.quantity} x {sale.product_name})"
    db.session.delete(sale)
    log_activity("sale_deleted", description)
    db.session.commit()
    flash("Line item deleted and stock restored.", "success")

    remaining = Sale.query.filter_by(receipt_number=receipt_number).count()
    if remaining:
        return redirect(url_for("view_receipt", receipt_number=receipt_number))
    return redirect(url_for("sales"))


# ---------------------------------------------------------------------------
# Products / Inventory
# ---------------------------------------------------------------------------

@app.route("/products")
@login_required
def products():
    search_form = ProductSearchForm(request.args)
    search_form.category.choices = [("", "All categories")] + [
        (c, c) for c in _distinct_categories()
    ]

    query = Product.query
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    status = request.args.get("status", "").strip()

    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(Product.name.ilike(like), Product.sku.ilike(like), Product.barcode.ilike(like))
        )
    if category:
        query = query.filter(Product.category == category)
    if status == "low":
        query = query.filter(Product.stock_quantity > 0, Product.stock_quantity <= Product.low_stock_threshold)
    elif status == "out":
        query = query.filter(Product.stock_quantity <= 0)

    all_products = query.order_by(Product.name).all()
    return render_template(
        "products.html",
        products=all_products,
        search_form=search_form,
        q=q,
        category=category,
        status=status,
    )


@app.route("/products/new", methods=["GET", "POST"])
@login_required
@role_required("admin", "manager")
def add_product():
    form = ProductForm()
    form.supplier_id.choices = _supplier_choices()
    if request.method == "GET":
        form.cost_price.data = 0.0
        form.tax_rate.data = app.config["DEFAULT_TAX_RATE"]
        form.low_stock_threshold.data = app.config["LOW_STOCK_THRESHOLD_DEFAULT"]

    if form.validate_on_submit():
        existing = Product.query.filter_by(name=form.name.data.strip()).first()
        if existing:
            flash("A product with that name already exists.", "error")
            return render_template("product_form.html", form=form, mode="add", categories=_distinct_categories())

        sku = (form.sku.data or "").strip().upper() or _unique_sku(form.category.data)
        if Product.query.filter_by(sku=sku).first():
            flash("That SKU is already in use.", "error")
            return render_template("product_form.html", form=form, mode="add", categories=_distinct_categories())

        barcode = (form.barcode.data or "").strip() or None
        if barcode and Product.query.filter_by(barcode=barcode).first():
            flash("That barcode is already in use by another product.", "error")
            return render_template("product_form.html", form=form, mode="add", categories=_distinct_categories())

        image_filename = _save_product_image(form.image.data)
        supplier_id = int(form.supplier_id.data) if form.supplier_id.data else None

        product = Product(
            name=form.name.data.strip(),
            sku=sku,
            barcode=barcode,
            category=(form.category.data or "").strip() or None,
            brand=(form.brand.data or "").strip() or None,
            description=(form.description.data or "").strip() or None,
            image_filename=image_filename,
            supplier_id=supplier_id,
            cost_price=form.cost_price.data or 0.0,
            selling_price=form.selling_price.data,
            tax_rate=form.tax_rate.data or 0.0,
            stock_quantity=form.stock_quantity.data,
            low_stock_threshold=form.low_stock_threshold.data,
        )
        db.session.add(product)
        log_activity("product_created", f"Added product '{product.name}' ({sku})")
        db.session.commit()
        flash(f"Product '{product.name}' added.", "success")
        return redirect(url_for("products"))

    return render_template("product_form.html", form=form, mode="add", categories=_distinct_categories())


@app.route("/products/edit/<int:product_id>", methods=["GET", "POST"])
@login_required
@role_required("admin", "manager")
def edit_product(product_id):
    product = db.session.get(Product, product_id)
    if product is None:
        abort(404)

    form = ProductForm(obj=product)
    form.supplier_id.choices = _supplier_choices()
    if request.method == "GET":
        form.supplier_id.data = str(product.supplier_id) if product.supplier_id else ""

    if form.validate_on_submit():
        duplicate = Product.query.filter(
            Product.name == form.name.data.strip(), Product.id != product.id
        ).first()
        if duplicate:
            flash("Another product already uses that name.", "error")
            return render_template("product_form.html", form=form, mode="edit", product=product, categories=_distinct_categories())

        sku = (form.sku.data or "").strip().upper() or product.sku
        sku_conflict = Product.query.filter(Product.sku == sku, Product.id != product.id).first()
        if sku_conflict:
            flash("That SKU is already in use by another product.", "error")
            return render_template("product_form.html", form=form, mode="edit", product=product, categories=_distinct_categories())

        barcode = (form.barcode.data or "").strip() or None
        if barcode:
            barcode_conflict = Product.query.filter(
                Product.barcode == barcode, Product.id != product.id
            ).first()
            if barcode_conflict:
                flash("That barcode is already in use by another product.", "error")
                return render_template("product_form.html", form=form, mode="edit", product=product, categories=_distinct_categories())

        stock_before = product.stock_quantity

        if form.image.data and getattr(form.image.data, "filename", ""):
            new_filename = _save_product_image(form.image.data)
            if new_filename:
                _delete_product_image(product.image_filename)
                product.image_filename = new_filename

        product.name = form.name.data.strip()
        product.sku = sku
        product.barcode = barcode
        product.category = (form.category.data or "").strip() or None
        product.brand = (form.brand.data or "").strip() or None
        product.description = (form.description.data or "").strip() or None
        product.supplier_id = int(form.supplier_id.data) if form.supplier_id.data else None
        product.cost_price = form.cost_price.data or 0.0
        product.selling_price = form.selling_price.data
        product.tax_rate = form.tax_rate.data or 0.0
        product.stock_quantity = form.stock_quantity.data
        product.low_stock_threshold = form.low_stock_threshold.data

        change_note = ""
        if stock_before != product.stock_quantity:
            change_note = f" (stock {stock_before} \u2192 {product.stock_quantity})"
        log_activity("product_edited", f"Updated product '{product.name}'{change_note}")

        db.session.commit()
        flash(f"Product '{product.name}' updated.", "success")
        return redirect(url_for("products"))

    return render_template("product_form.html", form=form, mode="edit", product=product, categories=_distinct_categories())


@app.route("/products/delete/<int:product_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def delete_product(product_id):
    product = db.session.get(Product, product_id)
    if product is None:
        abort(404)

    if product.sales:
        flash(
            f"Can't delete '{product.name}' \u2014 it has {len(product.sales)} sale(s) on record. "
            "Set its stock to 0 instead.",
            "error",
        )
        return redirect(url_for("products"))

    name = product.name
    _delete_product_image(product.image_filename)
    db.session.delete(product)
    log_activity("product_deleted", f"Deleted product '{name}'")
    db.session.commit()
    flash("Product deleted.", "success")
    return redirect(url_for("products"))


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

@app.route("/customers")
@login_required
def customers():
    q = request.args.get("q", "").strip()
    query = Customer.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(Customer.full_name.ilike(like), Customer.phone.ilike(like), Customer.email.ilike(like))
        )
    all_customers = query.order_by(Customer.full_name).all()
    return render_template("customers.html", customers=all_customers, q=q)


@app.route("/customers/new", methods=["GET", "POST"])
@login_required
@role_required("admin", "manager")
def add_customer():
    form = CustomerForm()
    if form.validate_on_submit():
        customer = Customer(
            full_name=form.full_name.data.strip(),
            phone=(form.phone.data or "").strip() or None,
            email=(form.email.data or "").strip() or None,
            address=(form.address.data or "").strip() or None,
            city=(form.city.data or "").strip() or None,
            country=(form.country.data or "").strip() or None,
            customer_type=form.customer_type.data,
        )
        db.session.add(customer)
        log_activity("customer_created", f"Added customer '{customer.full_name}'")
        db.session.commit()
        flash(f"Customer '{customer.full_name}' added.", "success")
        return redirect(url_for("customers"))
    return render_template("customer_form.html", form=form, mode="add")


@app.route("/customers/<int:customer_id>")
@login_required
def view_customer(customer_id):
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        abort(404)

    purchase_lines = (
        Sale.query.filter_by(customer_id=customer.id).order_by(Sale.date_created.desc()).limit(200).all()
    )
    purchases = _group_sales_into_receipts(purchase_lines)
    return render_template("customer_detail.html", customer=customer, purchases=purchases)


@app.route("/customers/edit/<int:customer_id>", methods=["GET", "POST"])
@login_required
@role_required("admin", "manager")
def edit_customer(customer_id):
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        abort(404)

    form = CustomerForm(obj=customer)
    if form.validate_on_submit():
        customer.full_name = form.full_name.data.strip()
        customer.phone = (form.phone.data or "").strip() or None
        customer.email = (form.email.data or "").strip() or None
        customer.address = (form.address.data or "").strip() or None
        customer.city = (form.city.data or "").strip() or None
        customer.country = (form.country.data or "").strip() or None
        customer.customer_type = form.customer_type.data
        log_activity("customer_edited", f"Updated customer '{customer.full_name}'")
        db.session.commit()
        flash(f"Customer '{customer.full_name}' updated.", "success")
        return redirect(url_for("customers"))
    return render_template("customer_form.html", form=form, mode="edit", customer=customer)


@app.route("/customers/delete/<int:customer_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def delete_customer(customer_id):
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        abort(404)

    name = customer.full_name
    for sale in list(customer.sales):
        sale.customer_id = None
    db.session.delete(customer)
    log_activity("customer_deleted", f"Deleted customer '{name}'")
    db.session.commit()
    flash("Customer deleted. Their past sales are kept on record as walk-in sales.", "success")
    return redirect(url_for("customers"))


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------

@app.route("/suppliers")
@login_required
def suppliers():
    q = request.args.get("q", "").strip()
    query = Supplier.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Supplier.name.ilike(like), Supplier.contact_person.ilike(like)))
    all_suppliers = query.order_by(Supplier.name).all()
    return render_template("suppliers.html", suppliers=all_suppliers, q=q)


@app.route("/suppliers/new", methods=["GET", "POST"])
@login_required
@role_required("admin", "manager")
def add_supplier():
    form = SupplierForm()
    if form.validate_on_submit():
        existing = Supplier.query.filter_by(name=form.name.data.strip()).first()
        if existing:
            flash("A supplier with that name already exists.", "error")
            return render_template("supplier_form.html", form=form, mode="add")

        supplier = Supplier(
            name=form.name.data.strip(),
            contact_person=(form.contact_person.data or "").strip() or None,
            phone=(form.phone.data or "").strip() or None,
            email=(form.email.data or "").strip() or None,
            address=(form.address.data or "").strip() or None,
            notes=(form.notes.data or "").strip() or None,
        )
        db.session.add(supplier)
        log_activity("supplier_created", f"Added supplier '{supplier.name}'")
        db.session.commit()
        flash(f"Supplier '{supplier.name}' added.", "success")
        return redirect(url_for("suppliers"))
    return render_template("supplier_form.html", form=form, mode="add")


@app.route("/suppliers/edit/<int:supplier_id>", methods=["GET", "POST"])
@login_required
@role_required("admin", "manager")
def edit_supplier(supplier_id):
    supplier = db.session.get(Supplier, supplier_id)
    if supplier is None:
        abort(404)

    form = SupplierForm(obj=supplier)
    if form.validate_on_submit():
        duplicate = Supplier.query.filter(
            Supplier.name == form.name.data.strip(), Supplier.id != supplier.id
        ).first()
        if duplicate:
            flash("Another supplier already uses that name.", "error")
            return render_template("supplier_form.html", form=form, mode="edit", supplier=supplier)

        supplier.name = form.name.data.strip()
        supplier.contact_person = (form.contact_person.data or "").strip() or None
        supplier.phone = (form.phone.data or "").strip() or None
        supplier.email = (form.email.data or "").strip() or None
        supplier.address = (form.address.data or "").strip() or None
        supplier.notes = (form.notes.data or "").strip() or None
        log_activity("supplier_edited", f"Updated supplier '{supplier.name}'")
        db.session.commit()
        flash(f"Supplier '{supplier.name}' updated.", "success")
        return redirect(url_for("suppliers"))
    return render_template("supplier_form.html", form=form, mode="edit", supplier=supplier)


@app.route("/suppliers/delete/<int:supplier_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def delete_supplier(supplier_id):
    supplier = db.session.get(Supplier, supplier_id)
    if supplier is None:
        abort(404)


    if supplier.products:
        flash(
            f"Can't delete '{supplier.name}' \u2014 {len(supplier.products)} product(s) are linked to it. "
            "Unassign them first.",
            "error",
        )
        return redirect(url_for("suppliers"))

    name = supplier.name
    db.session.delete(supplier)
    log_activity("supplier_deleted", f"Deleted supplier '{name}'")
    db.session.commit()
    flash("Supplier deleted.", "success")
    return redirect(url_for("suppliers"))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _filtered_sales_query():
    query = Sale.query
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    if start_date:
        query = query.filter(db.func.date(Sale.date_created) >= start_date)
    if end_date:
        query = query.filter(db.func.date(Sale.date_created) <= end_date)
    return query


@app.route("/reports")
@login_required
def reports():
    form = ReportFilterForm(request.args, meta={"csrf": False})
    filtered_sales = _filtered_sales_query().order_by(Sale.date_created.desc()).all()

    total_revenue = sum(s.total for s in filtered_sales)
    total_profit = sum(s.profit for s in filtered_sales)
    total_units = sum(s.quantity for s in filtered_sales)

    by_product = {}
    for s in filtered_sales:
        entry = by_product.setdefault(s.product_name, {"quantity": 0, "revenue": 0.0})
        entry["quantity"] += s.quantity
        entry["revenue"] += s.total
    best_sellers = sorted(by_product.items(), key=lambda item: item[1]["revenue"], reverse=True)[:5]

    return render_template(
        "reports.html",
        form=form,
        sales=filtered_sales,
        total_revenue=total_revenue,
        total_profit=total_profit,
        total_units=total_units,
        best_sellers=best_sellers,
        start_date=request.args.get("start_date", ""),
        end_date=request.args.get("end_date", ""),
    )


@app.route("/reports/export")
@login_required
def export_report():
    """Write the currently filtered sales to a CSV file in exports/ and send it."""
    filtered_sales = _filtered_sales_query().order_by(Sale.date_created.desc()).all()

    filename = f"sales_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    filepath = f"{app.config['EXPORTS_DIR']}/{filename}"

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["Receipt", "Date", "Product", "Quantity", "Unit Price", "Unit Cost", "Discount", "Tax", "Total", "Profit", "Customer", "Payment", "Sold By"]
        )
        for s in filtered_sales:
            writer.writerow(
                [
                    s.receipt_number,
                    s.date_created.strftime("%Y-%m-%d %H:%M"),
                    s.product_name,
                    s.quantity,
                    f"{s.unit_price:.2f}",
                    f"{s.unit_cost:.2f}",
                    f"{s.line_discount:.2f}",
                    f"{s.line_tax:.2f}",
                    f"{s.total:.2f}",
                    f"{s.profit:.2f}",
                    s.customer.full_name if s.customer else "",
                    s.payment_method_label,
                    s.sold_by or "",
                ]
            )

    return send_from_directory(app.config["EXPORTS_DIR"], filename, as_attachment=True)


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

@app.route("/backup", methods=["POST"])
@login_required
@role_required("admin", "manager")
def create_backup():
    """Dump every product, customer, supplier, and sale to a timestamped
    JSON file in backups/."""
    payload = {
        "created_at": datetime.now().isoformat(),
        "products": [
            {
                "name": p.name,
                "sku": p.sku,
                "barcode": p.barcode,
                "category": p.category,
                "brand": p.brand,
                "description": p.description,
                "supplier": p.supplier.name if p.supplier else None,
                "cost_price": p.cost_price,
                "selling_price": p.selling_price,
                "tax_rate": p.tax_rate,
                "stock_quantity": p.stock_quantity,
                "low_stock_threshold": p.low_stock_threshold,
            }
            for p in Product.query.all()
        ],
        "customers": [
            {
                "full_name": c.full_name,
                "phone": c.phone,
                "email": c.email,
                "address": c.address,
                "city": c.city,
                "country": c.country,
                "customer_type": c.customer_type,
            }
            for c in Customer.query.all()
        ],
        "suppliers": [
            {
                "name": s.name,
                "contact_person": s.contact_person,
                "phone": s.phone,
                "email": s.email,
                "address": s.address,
            }
            for s in Supplier.query.all()
        ],
        "sales": [
            {
                "receipt_number": s.receipt_number,
                "product_name": s.product_name,
                "quantity": s.quantity,
                "unit_price": s.unit_price,
                "unit_cost": s.unit_cost,
                "line_discount": s.line_discount,
                "line_tax": s.line_tax,
                "total": s.total,
                "customer": s.customer.full_name if s.customer else None,
                "payment_method": s.payment_method,
                "sold_by": s.sold_by,
                "date_created": s.date_created.isoformat(),
            }
            for s in Sale.query.all()
        ],
    }

    filename = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = f"{app.config['BACKUPS_DIR']}/{filename}"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    log_activity("backup_created", f"Created backup {filename}")
    db.session.commit()
    flash(f"Backup saved: {filename}", "success")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

@app.route("/activity-log")
@login_required
def activity_log():
    """Manual limit/offset pagination rather than Flask-SQLAlchemy's
    Query.paginate() - the exact paginate() API has shifted across
    Flask-SQLAlchemy versions, whereas limit()/offset() are stable
    everywhere."""
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = 40
    total = ActivityLog.query.count()
    total_pages = max((total + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    entries = (
        ActivityLog.query.order_by(ActivityLog.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return render_template(
        "activity_log.html",
        entries=entries,
        page=page,
        total_pages=total_pages,
        has_prev=page > 1,
        has_next=page < total_pages,
    )


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------

@app.route("/users")
@login_required
@role_required("admin")
def users():
    all_users = User.query.order_by(User.username).all()
    return render_template("users.html", users=all_users)


@app.route("/users/new", methods=["GET", "POST"])
@login_required
@role_required("admin")
def add_user():
    form = UserForm()
    if form.validate_on_submit():
        if not form.password.data:
            flash("A password is required for new accounts.", "error")
            return render_template("user_form.html", form=form, mode="add")

        existing = User.query.filter_by(username=form.username.data.strip()).first()
        if existing:
            flash("That username is already taken.", "error")
            return render_template("user_form.html", form=form, mode="add")

        user = User(username=form.username.data.strip(), role=form.role.data)
        user.set_password(form.password.data)
        db.session.add(user)
        log_activity("user_created", f"Created user '{user.username}' ({user.role_label})")
        db.session.commit()
        flash(f"User '{user.username}' created.", "success")
        return redirect(url_for("users"))

    return render_template("user_form.html", form=form, mode="add")


@app.route("/users/edit/<int:user_id>", methods=["GET", "POST"])
@login_required
@role_required("admin")
def edit_user(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)

    form = UserForm(obj=user)
    if request.method == "GET":
        form.password.data = ""  # never pre-fill a password field

    if form.validate_on_submit():
        duplicate = User.query.filter(
            User.username == form.username.data.strip(), User.id != user.id
        ).first()
        if duplicate:
            flash("That username is already taken.", "error")
            return render_template("user_form.html", form=form, mode="edit", edited_user=user)

        demoting_last_admin = (
            user.role == "admin"
            and form.role.data != "admin"
            and User.query.filter_by(role="admin").count() <= 1
        )
        if demoting_last_admin:
            flash("Can't change the role of the last administrator.", "error")
            return render_template("user_form.html", form=form, mode="edit", edited_user=user)

        user.username = form.username.data.strip()
        user.role = form.role.data
        if form.password.data:
            user.set_password(form.password.data)

        log_activity("user_edited", f"Updated user '{user.username}' ({user.role_label})")
        db.session.commit()
        flash(f"User '{user.username}' updated.", "success")
        return redirect(url_for("users"))

    return render_template("user_form.html", form=form, mode="edit", edited_user=user)


@app.route("/users/delete/<int:user_id>", methods=["POST"])
@login_required
@role_required("admin")
def delete_user(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)

    if user.id == current_user.id:
        flash("You can't delete your own account while logged in as it.", "error")
        return redirect(url_for("users"))

    if user.role == "admin" and User.query.filter_by(role="admin").count() <= 1:
        flash("Can't delete the last administrator.", "error")
        return redirect(url_for("users"))

    name = user.username
    db.session.delete(user)
    log_activity("user_deleted", f"Deleted user '{name}'")
    db.session.commit()
    flash("User deleted.", "success")
    return redirect(url_for("users"))


# ---------------------------------------------------------------------------
# Settings (admin only)
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
@role_required("admin")
def settings_page():
    settings = get_settings()
    form = SettingsForm(obj=settings)

    if form.validate_on_submit():
        settings.business_name = form.business_name.data.strip()
        settings.business_address = (form.business_address.data or "").strip() or None
        settings.business_phone = (form.business_phone.data or "").strip() or None
        settings.business_email = (form.business_email.data or "").strip() or None
        settings.currency_symbol = form.currency_symbol.data.strip()
        settings.default_tax_rate = form.default_tax_rate.data or 0.0
        settings.low_stock_threshold_default = form.low_stock_threshold_default.data or 0
        settings.receipt_footer = (form.receipt_footer.data or "").strip() or "Thank you for your business!"

        _sync_config_from_settings(settings)
        log_activity("settings_updated", "Updated business settings")
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings_page"))

    return render_template("settings.html", form=form)


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(403)
def forbidden(error):
    return render_template("403.html"), 403


@app.errorhandler(404)
def not_found(error):
    return render_template("404.html"), 404


@app.errorhandler(413)
def file_too_large(error):
    flash("That file is too large \u2014 please upload an image under 2 MB.", "error")
    return redirect(request.referrer or url_for("dashboard"))


@app.errorhandler(500)
def server_error(error):
    db.session.rollback()
    return render_template("500.html"), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(debug=True)
