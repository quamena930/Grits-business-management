"""
WTForms definitions.

Flask-WTF gives us CSRF protection for free and keeps validation rules in
one place instead of scattered across route functions in app.py.
"""

from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed
from wtforms import (
    StringField,
    PasswordField,
    SelectField,
    IntegerField,
    FloatField,
    TextAreaField,
    SubmitField,
    DateField,
)
from wtforms.validators import DataRequired, NumberRange, Length, Optional, Email

from models import PAYMENT_METHODS, ROLES

IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "gif")

CUSTOMER_TYPES = [("retail", "Retail"), ("wholesale", "Wholesale")]


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Log in")


class ProductForm(FlaskForm):
    # --- Identity ---
    name = StringField("Product name", validators=[DataRequired(), Length(max=120)])
    sku = StringField(
        "SKU",
        validators=[Optional(), Length(max=40)],
        description="Leave blank to auto-generate one.",
    )
    barcode = StringField("Barcode", validators=[Optional(), Length(max=64)])
    category = StringField("Category", validators=[Optional(), Length(max=80)])
    brand = StringField("Brand", validators=[Optional(), Length(max=80)])
    description = TextAreaField("Description", validators=[Optional(), Length(max=2000)])
    supplier_id = SelectField("Supplier", validators=[Optional()], choices=[], default="")
    image = FileField(
        "Product image",
        validators=[Optional(), FileAllowed(IMAGE_EXTENSIONS, "Images only (jpg, png, webp, gif).")],
    )

    # --- Pricing ---
    cost_price = FloatField(
        "Cost price", validators=[DataRequired(), NumberRange(min=0)], default=0.0
    )
    selling_price = FloatField(
        "Selling price", validators=[DataRequired(), NumberRange(min=0.01)]
    )
    tax_rate = FloatField(
        "Tax rate (%)", validators=[Optional(), NumberRange(min=0, max=100)], default=0.0
    )

    # --- Stock ---
    stock_quantity = IntegerField(
        "Quantity in stock", validators=[DataRequired(), NumberRange(min=0)]
    )
    low_stock_threshold = IntegerField(
        "Minimum stock (low-stock alert level)",
        validators=[DataRequired(), NumberRange(min=0)],
        default=5,
    )

    submit = SubmitField("Save product")


class ProductSearchForm(FlaskForm):
    """A simple GET-based filter form for the inventory list."""

    class Meta:
        csrf = False

    q = StringField("Search", validators=[Optional(), Length(max=120)])
    category = SelectField("Category", validators=[Optional()], choices=[], default="")
    status = SelectField(
        "Stock status",
        validators=[Optional()],
        choices=[("", "All stock levels"), ("low", "Low stock"), ("out", "Out of stock")],
        default="",
    )


class CartAddForm(FlaskForm):
    """Adds one product line to the in-progress cart (stored in the
    session, not the database, until checkout)."""

    product_id = SelectField("Product", coerce=int, validators=[DataRequired()])
    quantity = IntegerField("Quantity", validators=[DataRequired(), NumberRange(min=1)], default=1)
    submit = SubmitField("Add to cart")


class CheckoutForm(FlaskForm):
    """Finalises the cart into one or more Sale rows sharing a receipt
    number. customer_id of '0' means 'walk-in / no customer on file'."""

    customer_id = SelectField("Customer", validators=[Optional()], choices=[], default="0")
    payment_method = SelectField(
        "Payment method", validators=[DataRequired()], choices=PAYMENT_METHODS, default="cash"
    )
    discount_amount = FloatField(
        "Discount (optional)", validators=[Optional(), NumberRange(min=0)], default=0.0
    )
    submit = SubmitField("Complete sale")


class SaleForm(FlaskForm):
    # Choices are populated in the view at request time, from whatever
    # products currently exist - see app.py. Used to edit a single line
    # item within an existing receipt.
    product_id = SelectField("Product", coerce=int, validators=[DataRequired()])
    quantity = IntegerField("Quantity", validators=[DataRequired(), NumberRange(min=1)])
    submit = SubmitField("Save line item")


class CustomerForm(FlaskForm):
    full_name = StringField("Full name", validators=[DataRequired(), Length(max=120)])
    phone = StringField("Phone", validators=[Optional(), Length(max=30)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=120)])
    address = StringField("Address", validators=[Optional(), Length(max=255)])
    city = StringField("City", validators=[Optional(), Length(max=80)])
    country = StringField("Country", validators=[Optional(), Length(max=80)])
    customer_type = SelectField("Customer type", choices=CUSTOMER_TYPES, default="retail")
    submit = SubmitField("Save customer")


class CustomerSearchForm(FlaskForm):
    class Meta:
        csrf = False

    q = StringField("Search", validators=[Optional(), Length(max=120)])


class SupplierForm(FlaskForm):
    name = StringField("Supplier name", validators=[DataRequired(), Length(max=120)])
    contact_person = StringField("Contact person", validators=[Optional(), Length(max=120)])
    phone = StringField("Phone", validators=[Optional(), Length(max=30)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=120)])
    address = StringField("Address", validators=[Optional(), Length(max=255)])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=2000)])
    submit = SubmitField("Save supplier")


class ReportFilterForm(FlaskForm):
    """A simple GET-based filter form. CSRF is disabled here because GET
    requests don't change server state, so there's nothing to protect."""

    class Meta:
        csrf = False

    start_date = DateField("From", validators=[Optional()])
    end_date = DateField("To", validators=[Optional()])
    submit = SubmitField("Filter")


class UserForm(FlaskForm):
    """Shared by both 'add user' and 'edit user'. Password is optional so
    editing a user doesn't force a reset - app.py only changes the
    password if this field was actually filled in."""

    username = StringField("Username", validators=[DataRequired(), Length(max=80)])
    password = PasswordField(
        "Password",
        validators=[Optional(), Length(min=6, message="Use at least 6 characters.")],
        description="Leave blank to keep the current password when editing.",
    )
    role = SelectField("Role", choices=ROLES, validators=[DataRequired()])
    submit = SubmitField("Save user")


class SettingsForm(FlaskForm):
    business_name = StringField("Business name", validators=[DataRequired(), Length(max=120)])
    business_address = StringField("Address", validators=[Optional(), Length(max=255)])
    business_phone = StringField("Phone", validators=[Optional(), Length(max=30)])
    business_email = StringField("Email", validators=[Optional(), Email(), Length(max=120)])
    currency_symbol = StringField("Currency symbol", validators=[DataRequired(), Length(max=10)])
    default_tax_rate = FloatField(
        "Default tax rate (%) for new products", validators=[Optional(), NumberRange(min=0, max=100)]
    )
    low_stock_threshold_default = IntegerField(
        "Default low-stock alert level for new products", validators=[Optional(), NumberRange(min=0)]
    )
    receipt_footer = StringField("Receipt footer message", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Save settings")
