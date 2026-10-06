"""Isolated regression tests: no production database or live provider calls."""

import base64
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

os.environ["FLASK_DEBUG"] = "1"
os.environ["DATABASE_URI"] = "sqlite:///:memory:"
os.environ.pop("ENCRYPTION_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))

import jwt
import plaid
from app import app, create_app
from middleware.auth import validate_cloudflare_jwt
from models import (
    PlaidItems,
    SimpleFinCredentials,
    UserPlaidConfigs,
    db,
    hash_legacy_simplefin_passwords,
    hash_simplefin_password,
)
from services.simplefin_service import (
    format_amount,
    format_balance,
    map_plaid_account_to_simplefin,
    map_plaid_transaction_to_simplefin,
    to_epoch,
)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.context = app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()
        self.headers = {"Origin": "http://localhost"}
        self.user_id = "7335d417-61da-459d-899c-0a01c76a2f94"

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def credential(self):
        row = SimpleFinCredentials(
            user_id=self.user_id,
            claim_id="test-claim",
            username="test-user",
            password=hash_simplefin_password("test-password"),
            is_claimed=True,
        )
        db.session.add(row)
        db.session.commit()
        auth = base64.b64encode(b"test-user:test-password").decode()
        return {"Authorization": "Basic " + auth}

    def item(self):
        db.session.add(
            UserPlaidConfigs(
                user_id=self.user_id,
                user_email="test@example.com",
                plaid_client_id="client",
                plaid_secret="secret",
                plaid_env="sandbox",
            )
        )
        db.session.flush()
        row = PlaidItems(
            user_id=self.user_id, item_id="test-item", access_token="access"
        )
        db.session.add(row)
        db.session.commit()
        return row.id

    def test_config_rejects_non_string_fields(self):
        for field in ["clientID", "client_id", "secret", "env", "plaid_env"]:
            for value in [12, {}, [], True]:
                with self.subTest(field=field, value=value):
                    data = {"clientID": "client", "secret": "secret", field: value}
                    response = self.client.put(
                        "/api/user/plaid-config", json=data, headers=self.headers
                    )
                    self.assertEqual(response.status_code, 400)

    def test_invalid_environment_is_not_silently_changed(self):
        response = self.client.put(
            "/api/user/plaid-config",
            json={"clientID": "client", "secret": "secret", "env": "prodution"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)

    def test_token_exchange_rejects_invalid_metadata_before_upstream(self):
        for data in [
            {"public_token": "token", "institution_name": {}},
            {"public_token": "token", "institution_id": "a" * 65},
        ]:
            self.assertEqual(
                self.client.post(
                    "/api/plaid/exchange-public-token", json=data, headers=self.headers
                ).status_code,
                400,
            )

    def test_htmx_form_does_not_bypass_json_exchange_validation(self):
        response = self.client.post(
            "/api/plaid/exchange-public-token",
            data={"public_token": "token"},
            headers={**self.headers, "HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 400)

    def test_dates_reject_invalid_reversed_and_overflow_values(self):
        headers = self.credential()
        for query in [
            "start-date=abc",
            "end-date=abc",
            "start-date=2&end-date=0",
            "end-date=" + "9" * 100,
            "end-date=-62135596800",
        ]:
            with self.subTest(query=query):
                self.assertEqual(
                    self.client.get(
                        "/simplefin/accounts?" + query, headers=headers
                    ).status_code,
                    400,
                )

    def test_zero_epoch_is_honored(self):
        headers = self.credential()
        self.item()
        with patch("routes.simplefin.PlaidService.from_config") as factory:
            factory.return_value.get_accounts.return_value = []
            self.assertEqual(
                self.client.get(
                    "/simplefin/accounts?start-date=0&end-date=86400", headers=headers
                ).status_code,
                200,
            )
            args = factory.return_value.get_transactions.call_args.kwargs
            self.assertEqual(args["start_date"].isoformat(), "1970-01-01")
            self.assertEqual(args["end_date"].isoformat(), "1970-01-02")

    def test_failed_removal_preserves_local_access_token(self):
        item_id = self.item()
        error = plaid.ApiException(status=503, reason="Unavailable")
        error.body = '{"error_code":"INTERNAL_SERVER_ERROR"}'
        with patch("routes.plaid.PlaidService.from_config") as factory:
            factory.return_value.remove_item.side_effect = error
            response = self.client.delete(
                f"/api/plaid/items/{item_id}", headers=self.headers
            )
            self.assertEqual(response.status_code, 502)
            self.assertEqual(db.session.get(PlaidItems, item_id).access_token, "access")

    def test_already_removed_item_can_be_deleted_locally(self):
        item_id = self.item()
        error = plaid.ApiException(status=400)
        error.body = '{"error_code":"ITEM_NOT_FOUND"}'
        with patch("routes.plaid.PlaidService.from_config") as factory:
            factory.return_value.remove_item.side_effect = error
            self.assertEqual(
                self.client.delete(
                    f"/api/plaid/items/{item_id}", headers=self.headers
                ).status_code,
                200,
            )
            self.assertIsNone(db.session.get(PlaidItems, item_id))

    def test_claim_is_single_use_and_password_is_hashed(self):
        db.session.add(SimpleFinCredentials(user_id=self.user_id, claim_id="once"))
        db.session.commit()
        response = self.client.post("/simplefin/claim/once")
        self.assertEqual(response.status_code, 200)
        password = urlsplit(response.get_data(as_text=True)).password
        row = SimpleFinCredentials.query.filter_by(claim_id="once").one()
        self.assertNotEqual(row.password, password)
        self.assertTrue(row.check_password(password))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.post("/simplefin/claim/once").status_code, 403)

    def test_expiry_accepts_naive_and_aware_dates(self):
        now = datetime.now(timezone.utc)
        for created in [now, now.replace(tzinfo=None)]:
            self.assertFalse(
                SimpleFinCredentials(is_claimed=False, created_at=created).is_expired
            )
        self.assertTrue(
            SimpleFinCredentials(
                is_claimed=False, created_at=now - timedelta(days=2)
            ).is_expired
        )

    def test_legacy_password_migration(self):
        row = SimpleFinCredentials(
            user_id=self.user_id, claim_id="old", password="plaintext"
        )
        db.session.add(row)
        db.session.commit()
        hash_legacy_simplefin_passwords()
        self.assertTrue(row.check_password("plaintext"))
        hashed = row.password
        hash_legacy_simplefin_passwords()
        self.assertEqual(row.password, hashed)

    def test_jwt_expiration_claim_is_required(self):
        token = jwt.encode(
            {
                "sub": "user",
                "iss": "https://team.cloudflareaccess.com",
                "aud": "application",
            },
            "test-key" * 8,
            algorithm="HS256",
        )
        with (
            patch.dict(
                os.environ,
                {"CLOUDFLARE_TEAM_DOMAIN": "team", "CLOUDFLARE_AUD": "application"},
            ),
            patch("middleware.auth.get_jwks_client"),
            patch("middleware.auth.jwt.decode", wraps=jwt.decode) as decode,
        ):
            # Use a supported algorithm only inside the test to exercise claim enforcement.
            def decode_hs(token, key, **kwargs):
                kwargs["algorithms"] = ["HS256"]
                return jwt.api_jwt.decode(token, "test-key" * 8, **kwargs)

            decode.side_effect = decode_hs
            with self.assertRaises(jwt.MissingRequiredClaimError):
                validate_cloudflare_jwt(token)

    def test_csrf_and_secret_response_boundaries(self):
        self.assertEqual(
            self.client.post(
                "/simplefin/token", headers={"Origin": "https://attacker.example"}
            ).status_code,
            403,
        )
        self.item()
        response = self.client.get("/api/plaid/items")
        self.assertNotIn("access", response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        with patch.dict(
            os.environ, {"FLASK_DEBUG": "0", "FLASK_ENV": "production", "DEBUG": "0"}
        ):
            self.assertEqual(self.client.get("/api/plaid/items").status_code, 401)

    def test_factory_installs_routes_and_security_hooks(self):
        other = create_app()
        with other.test_client() as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(
                client.post(
                    "/simplefin/token", headers={"Origin": "https://attacker.example"}
                ).status_code,
                403,
            )
            with patch.dict(
                os.environ,
                {"FLASK_DEBUG": "0", "FLASK_ENV": "production", "DEBUG": "0"},
            ):
                self.assertEqual(client.get("/api/plaid/items").status_code, 401)

    def test_iso_datetime_preserves_time_and_offset(self):
        self.assertEqual(to_epoch("1970-01-01T02:30:00+02:00"), 1800)
        self.assertEqual(to_epoch("1970-01-01T00:30:00Z"), 1800)
        self.assertEqual(to_epoch("1970-01-01"), 0)

    def test_large_body_is_rejected(self):
        response = self.client.put(
            "/api/user/plaid-config", json={"secret": "x" * 70000}, headers=self.headers
        )
        self.assertEqual(response.status_code, 413)

    def test_available_credit_is_not_negated_as_debt(self):
        account = map_plaid_account_to_simplefin(
            {
                "account_id": "card",
                "type": "credit",
                "balances": {
                    "current": 200,
                    "available": 800,
                    "iso_currency_code": "USD",
                },
            }
        )
        self.assertEqual(account["balance"], "-200.00")
        self.assertEqual(account["available-balance"], "800.00")

    def test_transactions_always_supply_an_actual_compatible_payee(self):
        for values, expected in [
            ({"merchant_name": "Merchant", "name": "Bank description"}, "Merchant"),
            (
                {"merchant_name": None, "name": "Credit card purchase"},
                "Credit card purchase",
            ),
            ({"name": None, "original_description": "Bank memo"}, "Bank memo"),
            (
                {"merchant_name": "", "name": "", "original_description": ""},
                "Unknown payee",
            ),
        ]:
            with self.subTest(values=values):
                mapped = map_plaid_transaction_to_simplefin(
                    {
                        "transaction_id": "tx",
                        "date": "2026-10-01",
                        "amount": 10,
                        **values,
                    }
                )
                self.assertEqual(mapped["payee"], expected)
                self.assertEqual(mapped["amount"], "-10.00")

    def test_credit_card_history_with_missing_merchants_reconciles_to_current_balance(
        self,
    ):
        from decimal import Decimal

        headers = self.credential()
        self.item()
        account = {
            "account_id": "card",
            "name": "Plaid Credit Card",
            "type": "credit",
            "balances": {"current": 410, "available": None, "iso_currency_code": "USD"},
        }
        transactions = [
            {
                "account_id": "card",
                "transaction_id": "flight",
                "date": "2026-09-01",
                "amount": 12000,
                "name": "United Airlines",
                "merchant_name": None,
                "pending": False,
            },
            {
                "account_id": "card",
                "transaction_id": "purchases",
                "date": "2026-09-02",
                "amount": 471,
                "name": "Card purchases",
                "merchant_name": None,
                "pending": False,
            },
        ]
        with patch("routes.simplefin.PlaidService.from_config") as factory:
            factory.return_value.get_accounts.return_value = [account]
            factory.return_value.get_transactions.return_value = transactions
            response = self.client.get(
                "/simplefin/accounts?start-date=0&pending=1&account=card",
                headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        result = response.get_json()["accounts"][0]
        self.assertEqual(result["balance"], "-410.00")
        self.assertEqual(len(result["transactions"]), 2)
        self.assertTrue(all(tx.get("payee") for tx in result["transactions"]))
        history_total = sum(Decimal(tx["amount"]) for tx in result["transactions"])
        opening_balance = Decimal(result["balance"]) - history_total
        self.assertEqual(opening_balance, Decimal("12061.00"))
        self.assertEqual(opening_balance + history_total, Decimal("-410.00"))

    def test_money_signs_and_rounding(self):
        self.assertEqual(format_amount("12.505"), "-12.51")
        self.assertEqual(format_amount("0"), "0.00")
        self.assertEqual(format_balance("200", "credit"), "-200.00")
        self.assertEqual(format_balance("-12", "loan"), "12.00")


if __name__ == "__main__":
    unittest.main()
