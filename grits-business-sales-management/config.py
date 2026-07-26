"""
Application configuration.

Settings are read from environment variables where they exist, and fall
back to sensible development defaults otherwise. This means the app runs
out of the box with 'python app.py', but a real deployment can override
SECRET_KEY / DATABASE_URL / etc. without touching this file.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
EXPORTS_DIR = BASE_DIR / "exports"
BACKUPS_DIR = BASE_DIR / "backups"
PRODUCT_IMAGES_DIR = BASE_DIR / "static" / "images" / "products"

# Make sure the folders the app reads/writes actually exist before Flask
# (or SQLite) tries to use them.
for folder in (INSTANCE_DIR, EXPORTS_DIR, BACKUPS_DIR, PRODUCT_IMAGES_DIR):
    folder.mkdir(parents=True, exist_ok=True)


class Config:
    """Single configuration class - simple enough for a small business app."""

    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{INSTANCE_DIR / 'sales.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    EXPORTS_DIR = str(EXPORTS_DIR)
    BACKUPS_DIR = str(BACKUPS_DIR)
    PRODUCT_IMAGES_DIR = str(PRODUCT_IMAGES_DIR)

    # Shop-specific display settings.
    BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "Grits Business")
    BUSINESS_ADDRESS = os.environ.get("BUSINESS_ADDRESS", "")
    BUSINESS_PHONE = os.environ.get("BUSINESS_PHONE", "")
    BUSINESS_EMAIL = os.environ.get("BUSINESS_EMAIL", "")
    CURRENCY_SYMBOL = os.environ.get("CURRENCY_SYMBOL", "GH\u20b5")
    LOW_STOCK_THRESHOLD_DEFAULT = 5
    DEFAULT_TAX_RATE = float(os.environ.get("DEFAULT_TAX_RATE", "0"))
    RECEIPT_FOOTER = os.environ.get("RECEIPT_FOOTER", "Thank you for your business!")

    # Product image uploads.
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024  # 2 MB per request
    ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}

    WTF_CSRF_ENABLED = True
