"""Isolated regression tests: no production database or live provider calls."""

import base64
import contextlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

os.environ["FLASK_DEBUG"] = "1"
os.environ["DATABASE_URI"] = "sqlite:///:memory:"
os.environ["ENCRYPTION_KEY"] = base64.urlsafe_b64encode(os.urandom(32)).decode()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))

import gunicorn_conf
import jwt
import plaid
import requests
from app import app, create_app
from gunicorn.config import Config
from middleware.auth import validate_cloudflare_jwt
from models import (
    PlaidItems,
    SimpleFinCredentials,
    UserPlaidConfigs,
    db,
    hash_legacy_simplefin_passwords,
    hash_simplefin_password,
)
from services import routing_service
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

    def actual_users(self, users):
        """Points the bridge at a temporary Actual users file with the given content."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write(users if isinstance(users, str) else json.dumps(users))
        self.addCleanup(os.unlink, handle.name)
        return patch.dict(os.environ, {"ACTUAL_USERS_FILE": handle.name})

    def access_token(self, audience, email="alice@example.com"):
        return jwt.encode(
            {
                "sub": "user",
                "email": email,
                "iss": "https://team.cloudflareaccess.com",
                "aud": audience,
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            },
            "test-key" * 8,
            algorithm="HS256",
        )

    def production_access(self, **environ):
        """Production mode with Access tokens verified against a test signing key."""

        # Use a supported algorithm only inside the test to exercise claim enforcement.
        def decode_hs(token, key, **kwargs):
            kwargs["algorithms"] = ["HS256"]
            return jwt.api_jwt.decode(token, "test-key" * 8, **kwargs)

        stack = contextlib.ExitStack()
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "FLASK_DEBUG": "0",
                    "FLASK_ENV": "production",
                    "DEBUG": "0",
                    "CLOUDFLARE_TEAM_DOMAIN": "team",
                    "CLOUDFLARE_AUD": "dashboard",
                    **environ,
                },
            )
        )
        stack.enter_context(patch("middleware.auth.get_jwks_client"))
        stack.enter_context(patch("middleware.auth.jwt.decode", side_effect=decode_hs))
        return stack

    def test_route_names_the_container_assigned_to_the_user(self):
        # The development user is user@example.com; lookups ignore case
        with self.actual_users({"User@Example.com": "alice", "bob@example.com": "bob"}):
            response = self.client.get(
                "/auth/route", headers={"X-Actual-Upstream": "actual_bob"}
            )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["X-Actual-Upstream"], "actual_alice")

    def test_route_refuses_users_without_a_container(self):
        cases = [
            {"bob@example.com": "bob"},
            {"user@example.com": "../evil"},
            {"user@example.com": "host:80"},
            {"user@example.com": 5},
            "not json",
            "[]",
        ]
        for users in cases:
            with self.subTest(users=users), self.actual_users(users):
                response = self.client.get("/auth/route")
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("X-Actual-Upstream", response.headers)

        with patch.dict(os.environ, {"ACTUAL_USERS_FILE": "/nonexistent/users.json"}):
            self.assertEqual(self.client.get("/auth/route").status_code, 403)

    def test_route_requires_a_verified_access_token_in_production(self):
        with (
            self.actual_users({"alice@example.com": "alice"}),
            self.production_access(),
        ):
            self.assertEqual(self.client.get("/auth/route").status_code, 401)
            for token, expected in [
                (self.access_token("dashboard"), 204),
                (self.access_token("other-application"), 401),
                (self.access_token("dashboard", "mallory@example.com"), 403),
                ("not-a-token", 401),
            ]:
                response = self.client.get(
                    "/auth/route", headers={"Cf-Access-Jwt-Assertion": token}
                )
                self.assertEqual(response.status_code, expected)

    def test_service_token_common_name_cannot_select_a_human_container(self):
        with (
            self.actual_users({"alice@example.com": "alice"}),
            patch(
                "routes.routing.authenticate_request",
                return_value=(
                    {"sub": "service", "common_name": "alice@example.com"},
                    None,
                ),
            ),
        ):
            self.assertEqual(self.client.get("/auth/route").status_code, 403)

    def test_cloudflare_domain_rejects_url_authority_injection(self):
        from middleware.auth import get_normalized_team_domain

        for domain in (
            "evil.example/path",
            "user@team.cloudflareaccess.com",
            "team:443",
        ):
            with (
                self.subTest(domain=domain),
                patch.dict(os.environ, {"CLOUDFLARE_TEAM_DOMAIN": domain}),
                self.assertRaises(ValueError),
            ):
                get_normalized_team_domain()

    def test_route_and_dashboard_each_accept_only_their_own_application(self):
        actual, dashboard = (self.access_token(aud) for aud in ("actual", "dashboard"))
        with (
            self.actual_users({"alice@example.com": "alice"}),
            self.production_access(CLOUDFLARE_ACTUAL_AUD="actual"),
        ):
            for path, token, expected in [
                ("/auth/route", actual, 204),
                ("/auth/route", dashboard, 401),
                ("/api/plaid/items", dashboard, 200),
                ("/api/plaid/items", actual, 401),
            ]:
                response = self.client.get(
                    path, headers={"Cf-Access-Jwt-Assertion": token}
                )
                self.assertEqual(response.status_code, expected, (path, token))

    def test_self_service_gives_unlisted_users_a_generated_container(self):
        with (
            self.actual_users({"bob@example.com": "bob"}),
            patch.dict(os.environ, {"ACTUAL_SELF_SERVICE": "true"}),
        ):
            first = self.client.get("/auth/route").headers["X-Actual-Upstream"]
            again = self.client.get("/auth/route").headers["X-Actual-Upstream"]
            with patch.dict(os.environ, {"DEV_USER_EMAIL": "USER@example.com"}):
                same_person = self.client.get("/auth/route").headers
            with patch.dict(os.environ, {"DEV_USER_EMAIL": "bob@example.com"}):
                listed = self.client.get("/auth/route").headers["X-Actual-Upstream"]
            with patch.dict(os.environ, {"DEV_USER_EMAIL": "eve@example.com"}):
                other = self.client.get("/auth/route").headers["X-Actual-Upstream"]

        self.assertRegex(first, r"^actual_u-[0-9a-f]{16}$")
        self.assertEqual(first, again)
        self.assertEqual(first, same_person["X-Actual-Upstream"])
        self.assertEqual(listed, "actual_bob")
        self.assertNotEqual(first, other)

    def test_route_starts_the_container_through_the_provisioner(self):
        routing_service._last_ensured.clear()
        environ = {"ACTUAL_PROVISIONER_URL": "http://provisioner:8090/"}
        with (
            self.actual_users({"user@example.com": "alice"}),
            patch.dict(os.environ, environ),
            patch.object(routing_service.requests, "post") as post,
        ):
            post.return_value = Mock(status_code=200, ok=True)
            for _ in range(3):
                self.assertEqual(self.client.get("/auth/route").status_code, 204)

            # Asked once, then left alone until the heartbeat interval has passed
            post.assert_called_once()
            self.assertEqual(
                post.call_args.args[0],
                "http://provisioner:8090/containers/alice/ensure",
            )
            routing_service._last_ensured["alice"] -= 60
            self.client.get("/auth/route")
            self.assertEqual(post.call_count, 2)

    def test_route_reports_a_container_that_cannot_be_started(self):
        failures = [
            Mock(status_code=429, ok=False),
            Mock(status_code=502, ok=False),
            requests.ConnectionError("refused"),
        ]
        environ = {"ACTUAL_PROVISIONER_URL": "http://provisioner:8090"}
        with (
            self.actual_users({"user@example.com": "alice"}),
            patch.dict(os.environ, environ),
            patch.object(routing_service.requests, "post") as post,
        ):
            for failure in failures:
                routing_service._last_ensured.clear()
                post.side_effect = [failure]
                response = self.client.get("/auth/route")
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("X-Actual-Upstream", response.headers)

            # A failure is not remembered, so the next request tries again
            post.side_effect = [Mock(status_code=200, ok=True)]
            self.assertEqual(self.client.get("/auth/route").status_code, 204)

    def access_log_line(self, method, path, status="200 OK", query=""):
        """Returns what the production access log records for a request, if anything."""
        config = Config()
        config.set("accesslog", "-")
        config.set("access_log_format", gunicorn_conf.access_log_format)
        logger = gunicorn_conf.RedactingLogger(config)

        raw_uri = f"{path}?{query}" if query else path
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "RAW_URI": raw_uri,
            "SERVER_PROTOCOL": "HTTP/1.1",
            "REMOTE_ADDR": "172.28.0.50",
        }
        headers = [
            ("CF-CONNECTING-IP", "203.0.113.7"),
            ("AUTHORIZATION", "Basic " + base64.b64encode(b"sfin-user:pw").decode()),
        ]
        request = Mock(headers=headers)
        response = Mock(status=status, sent=0, headers=[])

        with patch.object(logger.access_log, "info") as info:
            logger.access(response, request, environ, timedelta(milliseconds=12))
        if not info.called:
            return None
        log_format, atoms = info.call_args.args
        return log_format % atoms

    def test_access_log_identifies_requests_without_recording_credentials(self):
        line = self.access_log_line(
            "POST", "/simplefin/claim/0123456789abcdef0123456789abcdef"
        )
        self.assertIn('"POST /simplefin/claim/[redacted]" 200', line)
        self.assertNotIn("0123456789abcdef", line)
        self.assertIn("172.28.0.50 203.0.113.7", line)

        line = self.access_log_line(
            "GET", "/simplefin/accounts", query="start-date=1&secret=hunter2"
        )
        self.assertIn('"GET /simplefin/accounts" 200', line)
        self.assertNotIn("hunter2", line)
        self.assertNotIn("sfin-user", line)

    def test_access_log_skips_only_successful_routing_checks(self):
        self.assertIsNone(self.access_log_line("GET", "/auth/route", "204 NO CONTENT"))
        self.assertIn(
            '"GET /auth/route" 403',
            self.access_log_line("GET", "/auth/route", "403 FORBIDDEN"),
        )
        self.assertIn('"GET /" 200', self.access_log_line("GET", "/"))


if __name__ == "__main__":
    unittest.main()
