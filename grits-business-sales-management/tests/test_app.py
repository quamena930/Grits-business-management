"""
Automated tests for the Grits Business Sales Management app.

Run with:
    python -m unittest discover tests
or, from the project root:
    python -m unittest tests.test_app -v

Uses a temporary on-disk SQLite database (not sqlite:///:memory:) so that
every connection Flask-SQLAlchemy opens during a test sees the same
database - an in-memory SQLite database is private to a single
connection, which is a well-known trap when a request handler and the
test's assertions end up on different connections.

No extra dependencies: everything here is Python's stdlib `unittest`
plus the packages the app itself already requires.
"""

import os
import tempfile
import unittest

from app import app as flask_app, _build_checkout_lines
from models import db, User, Product, Customer, Sale


class BaseTestCase(unittest.TestCase):
    """One temporary database for the whole test run; tables are cleared
    (not dropped/recreated) before each test for isolation. Recreating
    the schema per test risks Flask-SQLAlchemy reusing an engine cached
    against an earlier config; clearing rows sidesteps that entirely."""

    @classmethod
    def setUpClass(cls):
        cls._db_fd, cls._db_path = tempfile.mkstemp(suffix=".db")
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        flask_app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{cls._db_path}"
        cls.app_context = flask_app.app_context()
        cls.app_context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.app_context.pop()
        os.close(cls._db_fd)
        os.remove(cls._db_path)

    def setUp(self):
        self.client = flask_app.test_client()

        # Clear every table before each test so tests don't see each
        # other's data, without paying the cost of recreating the schema.
        for table in reversed(db.metadata.sorted_tables):
            db.session.execute(table.delete())
        db.session.commit()

        # One seeded user per role, all with the same test password.
        self.users = {}
        for role in ("admin", "manager", "cashier", "viewer"):
            user = User(username=f"{role}_user", role=role)
            user.set_password("password123")
            db.session.add(user)
            self.users[role] = user
        db.session.commit()
        for role, user in self.users.items():
            self.users[role] = user.id  # keep IDs; objects go stale after commit

    def login(self, role):
        return self.client.post(
            "/login",
            data={"username": f"{role}_user", "password": "password123"},
            follow_redirects=True,
        )


class AuthTests(BaseTestCase):
    def test_login_succeeds_with_correct_password(self):
        response = self.login("admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Dashboard", response.data)

    def test_login_fails_with_wrong_password(self):
        response = self.client.post(
            "/login",
            data={"username": "admin_user", "password": "wrong-password"},
            follow_redirects=True,
        )
        self.assertIn(b"Incorrect username or password", response.data)

    def test_dashboard_requires_login(self):
        response = self.client.get("/dashboard", follow_redirects=True)
        self.assertIn(b"Please log in to continue", response.data)


class CheckoutMathTests(BaseTestCase):
    """Tests _build_checkout_lines() directly - the discount/tax split is
    the single most important piece of business logic in the app, so it
    gets checked against hand-computed numbers rather than just
    exercised incidentally through an HTTP checkout."""

    def test_discount_and_tax_split_proportionally(self):
        product_a = Product(
            name="Test Product A", sku="TESTA", cost_price=6.0,
            selling_price=10.0, tax_rate=15.0, stock_quantity=100,
        )
        product_b = Product(
            name="Test Product B", sku="TESTB", cost_price=2.0,
            selling_price=5.0, tax_rate=0.0, stock_quantity=100,
        )
        db.session.add_all([product_a, product_b])
        db.session.commit()

        # Cart: 2 x Product A (subtotal 20) + 1 x Product B (subtotal 5)
        # = 25 subtotal, with a flat $5 discount applied at checkout.
        lines = _build_checkout_lines([(product_a, 2), (product_b, 1)], discount_amount=5.0)
        self.assertEqual(len(lines), 2)
        line_a, line_b = lines

        # Product A carries 20/25 = 80% of the subtotal, so 80% of the
        # discount: $4. Taxable = 20 - 4 = 16; 15% tax = $2.40.
        self.assertAlmostEqual(line_a["line_discount"], 4.0, places=2)
        self.assertAlmostEqual(line_a["line_tax"], 2.40, places=2)
        self.assertAlmostEqual(line_a["total"], 18.40, places=2)

        # Product B (the last line) absorbs whatever discount is left
        # over ($1), by design, so the two lines always sum exactly.
        self.assertAlmostEqual(line_b["line_discount"], 1.0, places=2)
        self.assertAlmostEqual(line_b["line_tax"], 0.0, places=2)
        self.assertAlmostEqual(line_b["total"], 4.0, places=2)

        total_discount = sum(l["line_discount"] for l in lines)
        self.assertAlmostEqual(total_discount, 5.0, places=2)

    def test_no_discount_no_tax_is_just_price_times_quantity(self):
        product = Product(
            name="Plain Product", sku="PLAIN1", cost_price=1.0,
            selling_price=3.0, tax_rate=0.0, stock_quantity=50,
        )
        db.session.add(product)
        db.session.commit()

        lines = _build_checkout_lines([(product, 4)], discount_amount=0.0)
        self.assertEqual(lines[0]["total"], 12.0)
        self.assertEqual(lines[0]["line_discount"], 0.0)
        self.assertEqual(lines[0]["line_tax"], 0.0)


class CheckoutFlowTests(BaseTestCase):
    """Exercises the real HTTP flow: add to cart, check out, verify
    stock and the resulting Sale row(s)."""

    def setUp(self):
        super().setUp()
        product = Product(
            name="Cart Test Widget", sku="CART1", cost_price=4.0,
            selling_price=10.0, tax_rate=10.0, stock_quantity=20,
            low_stock_threshold=5,
        )
        db.session.add(product)
        db.session.commit()
        self.product_id = product.id

    def test_full_checkout_reduces_stock_and_creates_receipt(self):
        self.login("cashier")

        response = self.client.post(
            "/sales/cart/add",
            data={"product_id": str(self.product_id), "quantity": "3"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            "/sales/checkout",
            data={"customer_id": "0", "payment_method": "cash", "discount_amount": "0"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)

        product = db.session.get(Product, self.product_id)
        self.assertEqual(product.stock_quantity, 17)  # 20 - 3

        sales = Sale.query.filter_by(product_id=self.product_id).all()
        self.assertEqual(len(sales), 1)
        sale = sales[0]
        self.assertEqual(sale.quantity, 3)
        # Subtotal 30, 10% tax = 3.00, no discount.
        self.assertAlmostEqual(sale.total, 33.0, places=2)
        self.assertTrue(sale.receipt_number.startswith("RCT-"))

        response = self.client.get(f"/sales/receipt/{sale.receipt_number}")
        self.assertEqual(response.status_code, 200)

    def test_cart_add_blocks_quantity_over_stock(self):
        self.login("cashier")
        response = self.client.post(
            "/sales/cart/add",
            data={"product_id": str(self.product_id), "quantity": "999"},
            follow_redirects=True,
        )
        self.assertIn(b"in stock", response.data)
        # Nothing should have been sold.
        self.assertEqual(Sale.query.filter_by(product_id=self.product_id).count(), 0)


class RolePermissionTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        product = Product(
            name="Permission Test Widget", sku="PERM1", cost_price=1.0,
            selling_price=5.0, stock_quantity=10,
        )
        db.session.add(product)
        db.session.commit()
        self.product_id = product.id

    def test_viewer_cannot_add_product(self):
        self.login("viewer")
        response = self.client.post("/products/new", data={}, follow_redirects=True)
        self.assertEqual(response.status_code, 403)

    def test_cashier_cannot_add_product(self):
        self.login("cashier")
        response = self.client.post("/products/new", data={}, follow_redirects=True)
        self.assertEqual(response.status_code, 403)

    def test_manager_can_add_product(self):
        self.login("manager")
        response = self.client.post(
            "/products/new",
            data={
                "name": "Manager Added Widget",
                "cost_price": "1",
                "selling_price": "5",
                "stock_quantity": "10",
                "low_stock_threshold": "5",
                "tax_rate": "0",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(Product.query.filter_by(name="Manager Added Widget").first())

    def test_cashier_cannot_delete_product(self):
        self.login("cashier")
        response = self.client.post(f"/products/delete/{self.product_id}", follow_redirects=True)
        self.assertEqual(response.status_code, 403)

    def test_viewer_cannot_access_settings(self):
        self.login("viewer")
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 403)

    def test_manager_cannot_access_settings(self):
        self.login("manager")
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 403)

    def test_admin_can_access_settings(self):
        self.login("admin")
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)

    def test_viewer_can_view_dashboard_and_sales_readonly(self):
        self.login("viewer")
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        self.assertEqual(self.client.get("/sales").status_code, 200)

    def test_viewer_cannot_add_to_cart(self):
        self.login("viewer")
        response = self.client.post(
            "/sales/cart/add",
            data={"product_id": str(self.product_id), "quantity": "1"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 403)


class UserManagementTests(BaseTestCase):
    def test_cannot_delete_own_account(self):
        self.login("admin")
        response = self.client.post(f"/users/delete/{self.users['admin']}", follow_redirects=True)
        self.assertIn(b"own account", response.data)
        self.assertIsNotNone(db.session.get(User, self.users["admin"]))

    def test_cannot_demote_last_admin(self):
        self.login("admin")
        response = self.client.post(
            f"/users/edit/{self.users['admin']}",
            data={"username": "admin_user", "role": "cashier", "password": ""},
            follow_redirects=True,
        )
        self.assertIn(b"last administrator", response.data)
        refreshed = db.session.get(User, self.users["admin"])
        self.assertEqual(refreshed.role, "admin")

    def test_admin_can_create_a_new_user(self):
        self.login("admin")
        response = self.client.post(
            "/users/new",
            data={"username": "new_cashier", "password": "somepassword", "role": "cashier"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        new_user = User.query.filter_by(username="new_cashier").first()
        self.assertIsNotNone(new_user)
        self.assertEqual(new_user.role, "cashier")


class CustomerTests(BaseTestCase):
    def test_customer_purchase_stats(self):
        product = Product(
            name="Customer Test Widget", sku="CUST1", cost_price=2.0,
            selling_price=8.0, stock_quantity=50,
        )
        customer = Customer(full_name="Ama Mensah", customer_type="retail")
        db.session.add_all([product, customer])
        db.session.commit()

        sale = Sale(
            receipt_number="RCT-TEST-0001",
            product_id=product.id,
            product_name=product.name,
            quantity=2,
            unit_price=8.0,
            unit_cost=2.0,
            total=16.0,
            customer_id=customer.id,
            payment_method="cash",
            sold_by="admin_user",
        )
        db.session.add(sale)
        db.session.commit()

        self.assertEqual(customer.purchase_count, 1)
        self.assertAlmostEqual(customer.total_spent, 16.0, places=2)

    def test_deleting_customer_keeps_sales_as_walk_in(self):
        product = Product(
            name="Widget For Deletion Test", sku="DELC1", cost_price=1.0,
            selling_price=4.0, stock_quantity=10,
        )
        customer = Customer(full_name="Kwame Owusu")
        db.session.add_all([product, customer])
        db.session.commit()

        sale = Sale(
            receipt_number="RCT-TEST-0002",
            product_id=product.id,
            product_name=product.name,
            quantity=1,
            unit_price=4.0,
            unit_cost=1.0,
            total=4.0,
            customer_id=customer.id,
            payment_method="cash",
            sold_by="admin_user",
        )
        db.session.add(sale)
        db.session.commit()
        customer_id, sale_id = customer.id, sale.id

        self.login("admin")
        self.client.post(f"/customers/delete/{customer_id}", follow_redirects=True)

        refreshed_sale = db.session.get(Sale, sale_id)
        self.assertIsNotNone(refreshed_sale)
        self.assertIsNone(refreshed_sale.customer_id)


if __name__ == "__main__":
    unittest.main()
