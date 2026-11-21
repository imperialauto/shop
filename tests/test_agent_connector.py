"""Claude connector: owner sign-in hands Claude a key without it passing through chat. Made-up data only."""
import base64
import hashlib
import os
import secrets
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi.testclient import TestClient

from test_agent_api import PASSWORD, _Setup

from app import agent_connector, agent_keys
from app.agent_keys import AgentAction, AgentKey
from app.main import app

CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class _Flow(_Setup):
    def setUp(self):
        super().setUp()
        agent_connector._used_codes.clear()

    def register(self, uris=(CALLBACK,)):
        return self.client.post("/internal/agent/oauth/register", json={"client_name": "Claude", "redirect_uris": list(uris)})

    def auth_params(self, client_id, challenge, scope=""):
        return {"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
                "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-123", "scope": scope}

    def approve(self, owner, client_id, challenge, scopes=None, decision="allow", days=90):
        data = dict(self.auth_params(client_id, challenge))
        data.update(decision=decision, days=str(days), scopes=scopes or list(agent_keys.SCOPES))
        return owner.post("/connect/authorize", data=data)

    def connect(self, scopes=None):
        client_id = self.register().json()["client_id"]
        verifier, challenge = pkce()
        owner = self.sign_in()
        back = urlparse(self.approve(owner, client_id, challenge, scopes).headers["location"])
        code = parse_qs(back.query)["code"][0]
        got = self.client.post("/internal/agent/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
            "client_id": client_id, "redirect_uri": CALLBACK})
        self.assertEqual(got.status_code, 200, got.text)
        return {"Authorization": "Bearer " + got.json()["access_token"]}, got.json()

    def rpc(self, headers, method, params=None, id_=1):
        return self.client.post("/internal/agent/mcp", headers=headers,
                                json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})


class DiscoveryTests(_Flow):
    def test_off_until_feature_is_on(self):
        with patch.dict(os.environ, {"AGENT_API_ENABLED": "false"}):
            for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server"):
                self.assertEqual(self.client.get(path).status_code, 404)
            self.assertEqual(self.register().status_code, 404)
            self.assertEqual(self.client.post("/internal/agent/mcp", json={}).status_code, 404)

    def test_metadata_points_at_this_dashboard(self):
        res = self.client.get("/.well-known/oauth-protected-resource").json()
        self.assertTrue(res["resource"].endswith("/internal/agent/mcp"))
        meta = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertTrue(meta["authorization_endpoint"].endswith("/connect/authorize"))
        self.assertEqual(meta["code_challenge_methods_supported"], ["S256"])

    def test_mcp_without_token_points_claude_to_sign_in(self):
        got = self.client.post("/internal/agent/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(got.status_code, 401)
        self.assertIn("resource_metadata=", got.headers["www-authenticate"])


class RegistrationTests(_Flow):
    def test_only_claude_callbacks_are_accepted(self):
        self.assertEqual(self.register(["https://evil.example/cb"]).status_code, 400)
        self.assertEqual(self.register([CALLBACK, "https://evil.example/cb"]).status_code, 400)
        self.assertEqual(self.register().status_code, 201)

    def test_forged_client_id_is_refused(self):
        _, challenge = pkce()
        owner = self.sign_in()
        page = owner.get("/connect/authorize?" + urlencode(self.auth_params("made-up", challenge)))
        self.assertEqual(page.status_code, 400)
        self.assertEqual(self.approve(owner, "made-up", challenge).status_code, 400)


class ConsentTests(_Flow):
    def test_signed_out_owner_goes_to_login_and_comes_back_to_the_whole_request(self):
        client_id = self.register().json()["client_id"]
        _, challenge = pkce()
        url = "/connect/authorize?" + urlencode(self.auth_params(client_id, challenge))
        browser = TestClient(app, follow_redirects=False)
        first = browser.get(url, headers={"accept": "text/html"})
        self.assertEqual(first.headers["location"], "/login")
        back = browser.post("/login", data={"username": "owner", "password": PASSWORD})
        self.assertEqual(parse_qs(urlparse(back.headers["location"]).query)["client_id"][0], client_id)
        page = browser.get(back.headers["location"])
        self.assertEqual(page.status_code, 200)
        self.assertIn("Allow", page.text)

    def test_allow_page_lets_the_browser_return_to_claude(self):
        client_id = self.register().json()["client_id"]
        _, challenge = pkce()
        page = self.sign_in().get("/connect/authorize?" + urlencode(self.auth_params(client_id, challenge)))
        policy = page.headers["content-security-policy"]
        form_action = next(p for p in policy.split(";") if p.strip().startswith("form-action"))
        self.assertIn("https://claude.ai", form_action)
        self.assertIn("frame-ancestors 'none'", policy)

    def test_only_the_owner_can_allow(self):
        client_id = self.register().json()["client_id"]
        _, challenge = pkce()
        office = self.sign_in("office")
        self.assertEqual(office.get("/connect/authorize?" + urlencode(self.auth_params(client_id, challenge))).status_code, 403)
        self.assertEqual(self.approve(office, client_id, challenge).status_code, 403)

    def test_deny_sends_claude_away_without_a_code(self):
        client_id = self.register().json()["client_id"]
        _, challenge = pkce()
        back = self.approve(self.sign_in(), client_id, challenge, decision="deny")
        query = parse_qs(urlparse(back.headers["location"]).query)
        self.assertEqual(query["error"], ["access_denied"])
        self.assertNotIn("code", query)


class TokenTests(_Flow):
    def test_full_connect_issues_a_hashed_connector_key(self):
        headers, body = self.connect()
        self.assertEqual(body["token_type"], "Bearer")
        raw = headers["Authorization"][7:]
        with self.Session() as db:
            key = db.query(AgentKey).one()
            self.assertEqual(key.name, agent_connector.CONNECTOR_NAME)
            self.assertNotIn(raw, (key.key_hash, key.key_prefix))

    def test_wrong_verifier_reused_or_tampered_codes_fail(self):
        client_id = self.register().json()["client_id"]
        verifier, challenge = pkce()
        back = self.approve(self.sign_in(), client_id, challenge)
        code = parse_qs(urlparse(back.headers["location"]).query)["code"][0]
        base = {"grant_type": "authorization_code", "client_id": client_id, "redirect_uri": CALLBACK}
        bad = self.client.post("/internal/agent/oauth/token", data={**base, "code": code, "code_verifier": "x" * 50})
        self.assertEqual(bad.json()["error"], "invalid_grant")
        bad = self.client.post("/internal/agent/oauth/token", data={**base, "code": code + "x", "code_verifier": verifier})
        self.assertEqual(bad.json()["error"], "invalid_grant")
        good = self.client.post("/internal/agent/oauth/token", data={**base, "code": code, "code_verifier": verifier})
        self.assertEqual(good.status_code, 200)
        again = self.client.post("/internal/agent/oauth/token", data={**base, "code": code, "code_verifier": verifier})
        self.assertEqual(again.json()["error"], "invalid_grant")

    def test_reconnecting_replaces_the_old_connector_key(self):
        old, _ = self.connect()
        new, _ = self.connect()
        self.assertEqual(self.rpc(old, "ping").status_code, 401)
        self.assertEqual(self.rpc(new, "ping").status_code, 200)
        with self.Session() as db:
            self.assertEqual(len(agent_keys.active_keys(db)), 1)


class McpTests(_Flow):
    def test_initialize_list_and_call(self):
        headers, _ = self.connect()
        init = self.rpc(headers, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "test", "version": "1"}}).json()
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(self.client.post("/internal/agent/mcp", headers=headers,
                                          json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code, 202)
        names = {t["name"] for t in self.rpc(headers, "tools/list").json()["result"]["tools"]}
        self.assertEqual(names, set(agent_connector.TOOLS))
        made = self.rpc(headers, "tools/call", {"name": "add_customer", "arguments": {
            "first_name": "Pat", "last_name": "Example", "phone": "(602) 555-0100"}}).json()["result"]
        self.assertFalse(made["isError"], made)
        found = self.rpc(headers, "tools/call", {"name": "find_customers", "arguments": {"query": "6025550100"}}).json()["result"]
        self.assertIn("Pat", found["content"][0]["text"])
        with self.Session() as db:
            self.assertEqual([a.action for a in db.query(AgentAction).all()], ["customer.create"])

    def test_scopes_limit_tools(self):
        headers, _ = self.connect(scopes=["customers:read"])
        names = {t["name"] for t in self.rpc(headers, "tools/list").json()["result"]["tools"]}
        self.assertEqual(names, {"find_customers", "get_customer"})
        refused = self.rpc(headers, "tools/call", {"name": "add_customer", "arguments": {
            "first_name": "Pat", "last_name": "Example"}}).json()["result"]
        self.assertTrue(refused["isError"])

    def test_bad_arguments_and_missing_records_are_tool_errors(self):
        headers, _ = self.connect()
        bad = self.rpc(headers, "tools/call", {"name": "get_customer", "arguments": {"customer_id": "x"}}).json()["result"]
        self.assertTrue(bad["isError"])
        missing = self.rpc(headers, "tools/call", {"name": "get_job", "arguments": {"job_id": 999}}).json()["result"]
        self.assertTrue(missing["isError"])
        self.assertIn("not found", missing["content"][0]["text"])

    def test_revoking_in_settings_cuts_claude_off(self):
        headers, _ = self.connect()
        with self.Session() as db:
            agent_keys.revoke(db, db.query(AgentKey).one().id, 1)
        self.assertEqual(self.rpc(headers, "tools/list").status_code, 401)


if __name__ == "__main__":
    unittest.main()
