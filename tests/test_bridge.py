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
    PlaidAccountSnapshots,
    PlaidItems,
    PlaidLinkSessions,
    SimpleFinCredentials,
    UserPlaidConfigs,
    db,
    hash_legacy_simplefin_passwords,
    hash_simplefin_password,
)
from services import routing_service
from services.plaid_service import oauth_redirect_uri, transaction_history_days
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

    def plaid_client(self):
        """Replaces the Plaid API client while keeping the real request building."""
        return patch("services.plaid_service.plaid_api.PlaidApi")

    def test_new_links_request_the_configured_transaction_history(self):
        self.item()
        cases = [({}, 730), ({"PLAID_TRANSACTION_HISTORY_DAYS": "180"}, 180)]
        for environ, expected in cases:
            with (
                self.subTest(environ=environ),
                patch.dict(os.environ, environ),
                self.plaid_client() as api,
            ):
                create = api.return_value.link_token_create
                create.return_value = {"link_token": "link-new"}
                response = self.client.post(
                    "/api/plaid/create-link-token", headers=self.headers
                )
                self.assertEqual(response.get_json()["link_token"], "link-new")

                sent = create.call_args.args[0].to_dict()
                self.assertEqual(sent["transactions"], {"days_requested": expected})
                self.assertEqual(sent["products"], ["transactions"])
                self.assertNotIn("access_token", sent)

    def test_invalid_transaction_history_setting_is_rejected(self):
        for value in ("0", "731", "-5", "two years", "1.5"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"PLAID_TRANSACTION_HISTORY_DAYS": value}),
                self.assertRaises(ValueError),
            ):
                transaction_history_days()

    def test_link_tokens_carry_the_redirect_address_when_one_is_set(self):
        item_id = self.item()
        uri = "https://bridge.example.com/oauth-return"
        paths = [
            "/api/plaid/create-link-token",
            f"/api/plaid/items/{item_id}/link-token",
        ]
        for path in paths:
            with self.subTest(path=path), self.plaid_client() as api:
                create = api.return_value.link_token_create
                create.return_value = {"link_token": "link"}

                self.client.post(path, headers=self.headers)
                self.assertNotIn("redirect_uri", create.call_args.args[0].to_dict())

                with patch.dict(os.environ, {"PLAID_REDIRECT_URI": uri}):
                    self.client.post(path, headers=self.headers)
                self.assertEqual(
                    create.call_args.args[0].to_dict()["redirect_uri"], uri
                )

    def test_linking_falls_back_to_a_popup_when_plaid_rejects_the_redirect_address(
        self,
    ):
        self.item()
        rejected = plaid.ApiException(status=400)
        rejected.body = (
            '{"error_code":"INVALID_FIELD","error_message":"OAuth redirect URI must '
            'be configured in the developer dashboard."}'
        )
        other = plaid.ApiException(status=400)
        other.body = '{"error_code":"INVALID_FIELD","error_message":"bad country code"}'
        environ = {"PLAID_REDIRECT_URI": "https://bridge.example.com/oauth-return"}

        with patch.dict(os.environ, environ), self.plaid_client() as api:
            create = api.return_value.link_token_create
            create.side_effect = [rejected, {"link_token": "link-popup"}]
            response = self.client.post(
                "/api/plaid/create-link-token", headers=self.headers
            )
            self.assertEqual(response.get_json()["link_token"], "link-popup")
            first, second = (call.args[0].to_dict() for call in create.call_args_list)
            self.assertIn("redirect_uri", first)
            self.assertNotIn("redirect_uri", second)
            self.assertEqual(second["products"], ["transactions"])

            # Any other rejection is reported, not retried
            create.reset_mock()
            create.side_effect = [other]
            response = self.client.post(
                "/api/plaid/create-link-token", headers=self.headers
            )
            self.assertEqual(response.status_code, 502)
            self.assertEqual(create.call_count, 1)

    def test_redirect_address_must_be_https_and_point_at_the_return_page(self):
        valid = [
            "https://bridge.example.com/oauth-return",
            "http://localhost:8080/oauth-return",
        ]
        invalid = [
            "http://bridge.example.com/oauth-return",
            "https://bridge.example.com/",
            "https://bridge.example.com/oauth-return/",
            "https://bridge.example.com/oauth-return?x=1",
            "bridge.example.com/oauth-return",
            "javascript:alert(1)",
        ]
        for uri in valid:
            with patch.dict(os.environ, {"PLAID_REDIRECT_URI": uri}):
                self.assertEqual(oauth_redirect_uri(), uri)
        for uri in invalid:
            with (
                self.subTest(uri=uri),
                patch.dict(os.environ, {"PLAID_REDIRECT_URI": uri}),
            ):
                with self.assertRaises(ValueError):
                    oauth_redirect_uri()
                # A bad setting stops the bridge at startup
                with self.assertRaises(ValueError):
                    create_app()
        self.assertIsNone(oauth_redirect_uri())

    def test_bank_return_page_serves_the_dashboard_to_signed_in_users_only(self):
        self.item()
        response = self.client.get("/oauth-return?oauth_state_id=abc")
        self.assertEqual(response.status_code, 200)
        self.assertIn("js/dashboard.js", response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

        with patch.dict(
            os.environ, {"FLASK_DEBUG": "0", "FLASK_ENV": "production", "DEBUG": "0"}
        ):
            self.assertEqual(
                self.client.get("/oauth-return?oauth_state_id=abc").status_code, 401
            )

    def test_reconnect_opens_update_mode_for_the_existing_connection(self):
        item_id = self.item()
        with self.plaid_client() as api:
            create = api.return_value.link_token_create
            create.return_value = {"link_token": "link-update"}
            response = self.client.post(
                f"/api/plaid/items/{item_id}/link-token", headers=self.headers
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["link_token"], "link-update")
            self.assertEqual(response.get_json()["item_id"], item_id)

            # Update mode names the connection and must not ask for products again
            sent = create.call_args.args[0].to_dict()
            self.assertEqual(sent["access_token"], "access")
            self.assertNotIn("products", sent)
            self.assertNotIn("transactions", sent)

        # Nothing about the stored connection changes
        item = db.session.get(PlaidItems, item_id)
        self.assertEqual((item.item_id, item.access_token), ("test-item", "access"))

    def test_reconnect_and_status_are_limited_to_the_users_own_institutions(self):
        item_id = self.item()
        with (
            self.plaid_client() as api,
            patch.dict(os.environ, {"DEV_USER_ID": "someone-else"}),
        ):
            for method, path in [
                ("post", f"/api/plaid/items/{item_id}/link-token"),
                ("get", f"/api/plaid/items/{item_id}/status"),
                ("post", "/api/plaid/items/9999/link-token"),
            ]:
                response = getattr(self.client, method)(path, headers=self.headers)
                self.assertEqual(response.status_code, 404, path)
            api.return_value.link_token_create.assert_not_called()
            api.return_value.item_get.assert_not_called()

        cross_site = self.client.post(
            f"/api/plaid/items/{item_id}/link-token",
            headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(cross_site.status_code, 403)

    def test_status_reports_the_health_of_a_connection(self):
        item_id = self.item()
        soon = datetime.now(timezone.utc) + timedelta(days=10)
        later = datetime.now(timezone.utc) + timedelta(days=200)
        login_required = plaid.ApiException(status=400)
        login_required.body = '{"error_code":"ITEM_LOGIN_REQUIRED"}'
        outage = plaid.ApiException(status=500)
        outage.body = '{"error_code":"INTERNAL_SERVER_ERROR"}'

        cases = [
            ({"item": {"error": None}}, "active", None),
            (
                {"item": {"error": None, "consent_expiration_time": later}},
                "active",
                "Access expires",
            ),
            (
                {"item": {"error": None, "consent_expiration_time": soon}},
                "expiring",
                "Access expires",
            ),
            (
                {"item": {"error": {"error_code": "ITEM_LOGIN_REQUIRED"}}},
                "login_required",
                "sign in again",
            ),
            ({"item": {"error": {"error_code": "NO_ACCOUNTS"}}}, "error", None),
            (login_required, "login_required", "sign in again"),
            (outage, "unknown", None),
            (TimeoutError("slow"), "unknown", None),
        ]
        for answer, state, detail in cases:
            with self.subTest(state=state, answer=answer), self.plaid_client() as api:
                if isinstance(answer, Exception):
                    api.return_value.item_get.side_effect = answer
                else:
                    api.return_value.item_get.return_value = answer
                response = self.client.get(f"/api/plaid/items/{item_id}/status")

                self.assertEqual(response.status_code, 200)
                body = response.get_json()
                self.assertEqual(body["state"], state)
                if detail:
                    self.assertIn(detail, body["detail"])
                else:
                    self.assertIsNone(body["detail"])

    def test_dashboard_shows_status_and_reconnect_for_each_institution(self):
        item_id = self.item()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn(f'hx-get="/api/plaid/items/{item_id}/status"', page)
        self.assertIn(f'data-reconnect-item="{item_id}"', page)

        with self.plaid_client() as api:
            api.return_value.item_get.return_value = {
                "item": {"error": {"error_code": "ITEM_LOGIN_REQUIRED"}}
            }
            badge = self.client.get(
                f"/api/plaid/items/{item_id}/status", headers={"HX-Request": "true"}
            ).get_data(as_text=True)
        self.assertIn("Reconnect required", badge)
        self.assertIn("badge-danger", badge)

    def test_sync_tells_actual_when_an_institution_needs_reconnecting(self):
        self.item()
        login_required = plaid.ApiException(status=400)
        login_required.body = '{"error_code":"ITEM_LOGIN_REQUIRED","error_message":"the login details changed"}'
        outage = plaid.ApiException(status=500)
        outage.body = (
            '{"error_code":"INTERNAL_SERVER_ERROR","error_message":"try later"}'
        )

        with patch("routes.simplefin.PlaidService.from_config") as factory:
            factory.return_value.get_accounts.side_effect = login_required
            errors = self.client.get(
                "/simplefin/accounts", headers=self.credential()
            ).get_json()["errors"]
            # Actual Budget recognises this opening phrase and flags the account
            self.assertEqual(len(errors), 1)
            self.assertTrue(
                errors[0].startswith("Connection to test-item may need attention")
            )
            self.assertIn("reconnect it in the bridge dashboard", errors[0])

            factory.return_value.get_accounts.side_effect = outage
            errors = self.client.get(
                "/simplefin/accounts",
                headers={"Authorization": "Basic dGVzdC11c2VyOnRlc3QtcGFzc3dvcmQ="},
            ).get_json()["errors"]
            self.assertEqual(
                errors, ["Plaid error for institution test-item: try later"]
            )

    def link_session(self, item_id=None):
        with self.plaid_client() as api:
            api.return_value.link_token_create.return_value = {
                "link_token": "link-owned"
            }
            path = (
                f"/api/plaid/items/{item_id}/link-token"
                if item_id
                else "/api/plaid/create-link-token"
            )
            response = self.client.post(path, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            return response.get_json()["session_id"]

    def test_link_continuation_and_exchange_reject_another_user_with_shared_client(
        self,
    ):
        self.item()
        session_id = self.link_session()
        db.session.add(
            UserPlaidConfigs(
                user_id="other",
                user_email="other@example.com",
                plaid_client_id="client",
                plaid_secret="secret",
                plaid_env="sandbox",
            )
        )
        db.session.commit()
        with (
            patch.dict(os.environ, {"DEV_USER_ID": "other"}),
            self.plaid_client() as api,
        ):
            self.assertEqual(
                self.client.get(f"/api/plaid/link-sessions/{session_id}").status_code,
                404,
            )
            self.assertEqual(
                self.client.delete(
                    f"/api/plaid/link-sessions/{session_id}", headers=self.headers
                ).status_code,
                404,
            )
            response = self.client.post(
                "/api/plaid/exchange-public-token",
                json={
                    "session_id": session_id,
                    "public_token": "public-from-first-user",
                },
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 404)
            api.return_value.item_public_token_exchange.assert_not_called()
        self.assertFalse(db.session.get(PlaidLinkSessions, session_id).completed)
        self.assertEqual(PlaidItems.query.filter_by(user_id="other").count(), 0)

    def test_link_exchange_requires_session_and_can_retry_completed_response(self):
        self.item()
        session_id = self.link_session()
        with self.plaid_client() as api:
            api.return_value.item_public_token_exchange.return_value = {
                "access_token": "new-access",
                "item_id": "new-item",
            }
            body = {"public_token": "public", "session_id": session_id}
            self.assertEqual(
                self.client.post(
                    "/api/plaid/exchange-public-token",
                    json={"public_token": "public"},
                    headers=self.headers,
                ).status_code,
                400,
            )
            api.return_value.item_public_token_exchange.assert_not_called()
            for expected in (201, 200):
                self.assertEqual(
                    self.client.post(
                        "/api/plaid/exchange-public-token",
                        json=body,
                        headers=self.headers,
                    ).status_code,
                    expected,
                )
            self.assertEqual(api.return_value.item_public_token_exchange.call_count, 1)
        self.assertEqual(PlaidItems.query.filter_by(item_id="new-item").count(), 1)

    def test_expired_or_changed_config_session_cannot_continue(self):
        self.item()
        session_id = self.link_session()
        session = db.session.get(PlaidLinkSessions, session_id)
        self.assertEqual(
            self.client.get(f"/api/plaid/link-sessions/{session_id}").get_json()[
                "link_token"
            ],
            "link-owned",
        )
        session.expires_at = datetime.now(timezone.utc).replace(
            tzinfo=None
        ) - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(
            self.client.get(f"/api/plaid/link-sessions/{session_id}").status_code, 410
        )
        session_id = self.link_session()
        db.session.get(UserPlaidConfigs, self.user_id).plaid_client_id = "new-client"
        db.session.commit()
        self.assertEqual(
            self.client.get(f"/api/plaid/link-sessions/{session_id}").status_code, 409
        )

    def test_failed_exchange_releases_reservation_for_retry(self):
        self.item()
        session_id = self.link_session()
        error = plaid.ApiException(status=503)
        error.body = '{"error_code":"INTERNAL_SERVER_ERROR"}'
        with self.plaid_client() as api:
            api.return_value.item_public_token_exchange.side_effect = [
                error,
                {"access_token": "new-access", "item_id": "new-item"},
            ]
            body = {"public_token": "public", "session_id": session_id}
            self.assertEqual(
                self.client.post(
                    "/api/plaid/exchange-public-token", json=body, headers=self.headers
                ).status_code,
                502,
            )
            self.assertFalse(
                db.session.get(PlaidLinkSessions, session_id).exchange_started
            )
            self.assertEqual(
                self.client.post(
                    "/api/plaid/exchange-public-token", json=body, headers=self.headers
                ).status_code,
                201,
            )

    def test_in_progress_exchange_cannot_be_replayed_or_cancelled(self):
        self.item()
        session_id = self.link_session()
        session = db.session.get(PlaidLinkSessions, session_id)
        session.exchange_started = True
        db.session.commit()
        with self.plaid_client() as api:
            self.assertEqual(
                self.client.post(
                    "/api/plaid/exchange-public-token",
                    json={"public_token": "public", "session_id": session_id},
                    headers=self.headers,
                ).status_code,
                409,
            )
            self.assertEqual(
                self.client.delete(
                    f"/api/plaid/link-sessions/{session_id}", headers=self.headers
                ).status_code,
                409,
            )
            api.return_value.item_public_token_exchange.assert_not_called()

    def test_reconnect_completion_and_cancel_are_owned_and_do_not_exchange(self):
        item_id = self.item()
        session_id = self.link_session(item_id)
        with patch.dict(os.environ, {"DEV_USER_ID": "other"}):
            self.assertEqual(
                self.client.post(
                    f"/api/plaid/link-sessions/{session_id}/complete",
                    headers=self.headers,
                ).status_code,
                404,
            )
        with self.plaid_client() as api:
            self.assertEqual(
                self.client.post(
                    "/api/plaid/exchange-public-token",
                    json={"public_token": "unused", "session_id": session_id},
                    headers=self.headers,
                ).status_code,
                400,
            )
            self.assertEqual(
                self.client.post(
                    f"/api/plaid/link-sessions/{session_id}/complete",
                    headers=self.headers,
                ).status_code,
                204,
            )
            api.return_value.item_public_token_exchange.assert_not_called()
        self.assertTrue(db.session.get(PlaidLinkSessions, session_id).completed)
        self.assertEqual(
            self.client.delete(
                f"/api/plaid/link-sessions/{session_id}", headers=self.headers
            ).status_code,
            204,
        )
        self.assertIsNone(db.session.get(PlaidLinkSessions, session_id))
        self.assertEqual(db.session.get(PlaidItems, item_id).access_token, "access")

    def test_reconnect_failure_uses_real_snapshot_with_original_timestamp(self):
        item_id = self.item()
        auth = self.credential()
        account = {
            "account_id": "acct",
            "name": "Checking",
            "type": "depository",
            "balances": {"current": 123.45, "iso_currency_code": "USD"},
        }
        error = plaid.ApiException(status=400)
        error.body = '{"error_code":"ITEM_LOGIN_REQUIRED"}'
        with patch("routes.simplefin.PlaidService.from_config") as factory:
            factory.return_value.get_accounts.return_value = [account]
            factory.return_value.get_transactions.return_value = []
            original = self.client.get("/simplefin/accounts", headers=auth).get_json()[
                "accounts"
            ]
            self.assertEqual(original[0]["balance"], "123.45")
            self.assertIsNotNone(db.session.get(PlaidAccountSnapshots, item_id))
            factory.return_value.get_accounts.side_effect = error
            response = self.client.get("/simplefin/accounts", headers=auth)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["accounts"], original)
            org = original[0]["org"]["name"]
            self.assertTrue(
                any(
                    e.startswith(f"Connection to {org} may need attention")
                    for e in response.get_json()["errors"]
                )
            )
            with patch.dict(os.environ, {"DEV_USER_ID": "other"}):
                self.assertEqual(self.client.get("/api/plaid/items").get_json(), [])

    def test_reconnect_failure_without_snapshot_fails_instead_of_reporting_deletion(
        self,
    ):
        self.item()
        error = plaid.ApiException(status=400)
        error.body = '{"error_code":"ITEM_LOGIN_REQUIRED"}'
        with patch("routes.simplefin.PlaidService.from_config") as factory:
            factory.return_value.get_accounts.side_effect = error
            response = self.client.get("/simplefin/accounts", headers=self.credential())
        self.assertEqual(response.status_code, 503)
        self.assertIn("reconnect", response.get_json()["errors"][0])
        self.assertEqual(response.get_json()["accounts"], [])

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
        environ = {"ACTUAL_SELF_SERVICE": "true", "COMPOSE_PROFILES": "provisioner"}
        with (
            self.actual_users({"bob@example.com": "bob"}),
            patch.dict(os.environ, environ),
            patch.object(routing_service.requests, "post") as post,
        ):
            post.return_value = Mock(status_code=200, ok=True)
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

    def test_provisioner_is_off_unless_its_profile_is_enabled(self):
        routing_service._last_ensured.clear()
        with (
            self.actual_users({"user@example.com": "alice"}),
            patch.object(routing_service.requests, "post") as post,
        ):
            post.return_value = Mock(status_code=200, ok=True)

            # Off by default: the container is expected to be running already
            for profiles in ("", "production-only", "provisioners"):
                with patch.dict(os.environ, {"COMPOSE_PROFILES": profiles}):
                    response = self.client.get("/auth/route")
                    self.assertEqual(
                        response.headers["X-Actual-Upstream"], "actual_alice"
                    )
            post.assert_not_called()

            with patch.dict(os.environ, {"COMPOSE_PROFILES": "other, provisioner"}):
                self.assertEqual(self.client.get("/auth/route").status_code, 204)
            self.assertEqual(
                post.call_args.args[0],
                "http://provisioner:8090/containers/alice/ensure",
            )

    def test_self_service_needs_the_provisioner(self):
        # Without the provisioner nothing could create a container for the user
        with (
            self.actual_users({"bob@example.com": "bob"}),
            patch.dict(os.environ, {"ACTUAL_SELF_SERVICE": "true"}),
        ):
            response = self.client.get("/auth/route")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("X-Actual-Upstream", response.headers)

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
