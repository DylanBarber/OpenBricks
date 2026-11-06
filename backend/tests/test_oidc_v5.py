"""Actual HTTP provider round trips exercise serialization and JWT verification."""

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import importlib.util
from pathlib import Path
import sys
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import jwt
import pytest

from openbricks.oidc import (
    OIDCConfig,
    OIDCError,
    OIDCLogin,
    OIDCAccessLogFilter,
    register_oidc_routes,
    return_path,
    synchronize_identity,
)
from openbricks.permissions import Access
from openbricks.store import Store


@pytest.fixture
def provider():
    class Provider:
        mode = "valid"
        codes = {}
        calls = []
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        kid = "fixture-key"

        def jwk(self):
            return {
                **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key())),
                "kid": self.kid,
                "use": "sig",
                "alg": "RS256",
            }

    state = Provider()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, body, status=200):
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            path = urlsplit(self.path)
            state.calls.append(("GET", path.path))
            if path.path == "/.well-known/openid-configuration":
                return self.send_json(
                    {
                        "issuer": state.url,
                        "authorization_endpoint": state.url + "/authorize",
                        "token_endpoint": state.url + "/token",
                        "jwks_uri": state.url + "/jwks",
                        "response_types_supported": ["code"],
                        "id_token_signing_alg_values_supported": ["RS256"],
                        "code_challenge_methods_supported": ["S256"],
                        "token_endpoint_auth_methods_supported": [
                            "client_secret_basic",
                            "none",
                        ],
                    }
                )
            if path.path == "/jwks":
                return self.send_json({"keys": [state.jwk()]})
            if path.path == "/authorize":
                values = {key: value[0] for key, value in parse_qs(path.query).items()}
                assert (
                    values["client_id"] == "fixture-client"
                    and values["code_challenge_method"] == "S256"
                )
                code = "one-use-code-" + str(len(state.codes))
                state.codes[code] = values
                self.send_response(302)
                self.send_header(
                    "Location",
                    values["redirect_uri"]
                    + "?"
                    + urlencode({"code": code, "state": values["state"]}),
                )
                self.end_headers()
                return
            self.send_json({"error": "not_implemented"}, 501)

        def do_POST(self):
            state.calls.append(("POST", self.path))
            if self.path != "/token":
                return self.send_json({"error": "not_implemented"}, 501)
            if state.mode == "provider_error":
                return self.send_json({"error": "provider-unavailable"}, 500)
            values = {
                key: value[0]
                for key, value in parse_qs(
                    self.rfile.read(int(self.headers["Content-Length"])).decode()
                ).items()
            }
            original = state.codes.pop(values.get("code"), None)
            if not original:
                return self.send_json({"error": "invalid_grant"}, 400)
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(values["code_verifier"].encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            assert (
                values["grant_type"] == "authorization_code"
                and values["redirect_uri"] == original["redirect_uri"]
            )
            assert challenge == original["code_challenge"]
            assert (
                self.headers["Authorization"]
                == "Basic "
                + base64.b64encode(b"fixture-client:fixture-client-secret").decode()
            )
            claims = {
                "iss": state.url,
                "sub": "stable-provider-subject",
                "aud": "fixture-client",
                "exp": int(time.time()) + 180,
                "iat": int(time.time()),
                "nonce": original["nonce"],
                "groups": ["fixture-editors"],
                "name": "Example SSO member",
                "email": "local-admin@example.invalid",
            }
            overrides = {
                "bad_nonce": {"nonce": "different-nonce"},
                "bad_audience": {"aud": "other-client"},
                "bad_issuer": {"iss": "https://untrusted.example.invalid"},
                "expired": {"exp": int(time.time()) - 300},
                "future": {"iat": int(time.time()) + 600},
                "old_issued": {"iat": int(time.time()) - 600},
                "bad_azp": {"azp": "different-client"},
                "missing_groups": {"groups": []},
                "bad_groups": {"groups": "fixture-editors"},
                "bad_hash": {"at_hash": "wrong-hash"},
                "string_expiry": {"exp": str(int(time.time()) + 180)},
            }
            claims.update(overrides.get(state.mode, {}))
            key = (
                rsa.generate_private_key(public_exponent=65537, key_size=2048)
                if state.mode == "bad_signature"
                else state.key
            )
            token = jwt.encode(
                claims, key, algorithm="RS256", headers={"kid": state.kid}
            )
            self.send_json(
                {
                    "id_token": token,
                    "access_token": "fixture-provider-access-token",
                    "token_type": "Bearer",
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def configuration(monkeypatch, provider):
    monkeypatch.setenv("OPENBRICKS_OIDC_ISSUER", provider.url)
    monkeypatch.setenv("OPENBRICKS_OIDC_CLIENT_ID", "fixture-client")
    monkeypatch.setenv("OPENBRICKS_OIDC_CLIENT_SECRET", "fixture-client-secret")
    monkeypatch.setenv(
        "OPENBRICKS_OIDC_REDIRECT_URI", "http://testserver/api/auth/oidc/callback"
    )
    monkeypatch.setenv("OPENBRICKS_OIDC_ALLOW_HTTP", "true")
    monkeypatch.setenv("OPENBRICKS_OIDC_DEV_HTTP_HOSTS", "testserver")
    monkeypatch.setenv(
        "OPENBRICKS_OIDC_GROUP_ROLE_MAP",
        json.dumps(
            {
                "fixture-editors": {"default": "editor"},
                "fixture-readers": {"default": "viewer"},
            }
        ),
    )
    return OIDCConfig.from_environment()


def challenge(service):
    target, binding = service.begin("/notebooks?view=recent")
    response = httpx.get(target, follow_redirects=False, timeout=5)
    assert response.status_code == 302
    args = {
        key: values[0]
        for key, values in parse_qs(
            urlsplit(response.headers["Location"]).query
        ).items()
    }
    return args, binding


def test_real_http_code_exchange_pkce_claims_and_issuer_subject_mapping(
    configuration, provider, tmp_path
):
    service = OIDCLogin(configuration)
    args, binding = challenge(service)
    claims, target = service.complete(args["state"], binding, args["code"])
    assert target == "/notebooks?view=recent"
    assert ("POST", "/token") in provider.calls and ("GET", "/jwks") in provider.calls
    store = Store(tmp_path)
    Access(store).seed()
    store.put(
        "users",
        {
            "id": "local-admin",
            "username": "local-admin@example.invalid",
            "role": "admin",
        },
    )
    account = synchronize_identity(store, configuration, claims)
    assert account["id"] != "local-admin" and account["role"] == "member"
    assert Access(store).role(account) == "editor"
    assert store.get("users", "local-admin")["role"] == "admin"
    claims["groups"] = ["fixture-readers"]
    synchronize_identity(store, configuration, claims)
    assert Access(store).role(account) == "viewer"
    with pytest.raises(OIDCError):
        service.complete(args["state"], binding, args["code"])


@pytest.mark.parametrize(
    "mode",
    [
        "bad_nonce",
        "bad_audience",
        "bad_issuer",
        "expired",
        "future",
        "old_issued",
        "bad_azp",
        "bad_groups",
        "bad_hash",
        "string_expiry",
        "bad_signature",
        "provider_error",
    ],
)
def test_provider_failures_do_not_authenticate(configuration, provider, mode):
    provider.mode = mode
    service = OIDCLogin(configuration)
    args, binding = challenge(service)
    with pytest.raises(OIDCError):
        service.complete(args["state"], binding, args["code"])
    with pytest.raises(OIDCError):
        service.complete(args["state"], binding, args["code"])


def test_browser_binding_groups_and_unsafe_redirects_fail_closed(
    configuration, tmp_path
):
    service = OIDCLogin(configuration)
    args, _ = challenge(service)
    with pytest.raises(OIDCError):
        service.complete(args["state"], "different-browser", args["code"])
    for value in (
        "https://evil.invalid",
        "//evil.invalid",
        "/\\evil.invalid",
        "/api/auth/login",
        "/\nlocation",
    ):
        with pytest.raises(OIDCError):
            return_path(value)
    store = Store(tmp_path)
    Access(store).seed()
    with pytest.raises(OIDCError):
        synchronize_identity(store, configuration, {"sub": "subject", "groups": []})
    with pytest.raises(OIDCError):
        configuration.endpoint("http://127.0.0.1:1/secret-service")


def test_jwks_key_rotation_refreshes_once(configuration, provider):
    service = OIDCLogin(configuration)
    args, binding = challenge(service)
    service._keys()
    provider.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider.kid = "rotated-fixture-key"
    assert (
        service.complete(args["state"], binding, args["code"])[0]["sub"]
        == "stable-provider-subject"
    )
    assert provider.calls.count(("GET", "/jwks")) == 2


def test_callback_creates_only_hashed_local_session_and_one_time_browser_cookie(
    configuration, provider, tmp_path
):
    app = FastAPI()
    app.state.store = Store(tmp_path)
    Access(app.state.store).seed()
    register_oidc_routes(app, config=configuration)
    with TestClient(app) as client:
        assert client.get("/api/auth/providers").json()["providers"][0]["id"] == "oidc"
        start = client.get("/api/auth/oidc/login", follow_redirects=False)
        authorization = httpx.get(start.headers["Location"], follow_redirects=False)
        path = urlsplit(authorization.headers["Location"])
        result = client.get(path.path + "?" + path.query, follow_redirects=False)
        assert result.status_code == 303 and result.headers["location"] == "/"
        assert "httponly" in result.headers["set-cookie"].lower()
        session = client.cookies.get("openbricks_session")
        record = app.state.store.get(
            "sessions", hashlib.sha256(session.encode()).hexdigest()
        )
        assert record and record["auth_provider"] == "oidc"
        assert session not in json.dumps(app.state.store.list("sessions"))
        serialized = json.dumps(
            app.state.store.list("sessions")
            + app.state.store.list("users")
            + app.state.store.list("audit_events")
        )
        assert (
            "fixture-client-secret" not in serialized
            and "fixture-provider-access-token" not in serialized
        )
        replay = client.get(path.path + "?" + path.query, follow_redirects=False)
        assert replay.headers["location"] == "/?sso_error=login_failed"


def test_invalid_or_disabled_configuration_preserves_local_login_surface(monkeypatch):
    monkeypatch.delenv("OPENBRICKS_OIDC_ISSUER", raising=False)
    app = FastAPI()
    register_oidc_routes(app)
    with TestClient(app) as client:
        assert client.get("/api/auth/providers").json()["providers"] == []
        assert client.get("/api/auth/oidc/login").status_code == 503
    monkeypatch.setenv("OPENBRICKS_OIDC_ISSUER", "http://untrusted.invalid")
    monkeypatch.delenv("OPENBRICKS_OIDC_ALLOW_HTTP", raising=False)
    app = FastAPI()
    register_oidc_routes(app)
    with TestClient(app) as client:
        assert client.get("/api/auth/providers").json()["oidc_misconfigured"] is True


def test_callback_codes_and_state_are_absent_from_standard_uvicorn_access_logs():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "test",
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1",
            "GET",
            "/api/auth/oidc/callback?code=fixture-code&state=fixture-state",
            "1.1",
            303,
        ),
        None,
    )
    assert OIDCAccessLogFilter().filter(record)
    assert (
        "fixture-code" not in record.getMessage()
        and "fixture-state" not in record.getMessage()
    )
    assert "/api/auth/oidc/callback" in record.getMessage()


def test_malformed_environment_role_is_a_safe_configuration_error(
    configuration, monkeypatch
):
    monkeypatch.setenv("OPENBRICKS_OIDC_GROUP_ROLE_MAP", '{"group":{"default":[]}}')
    with pytest.raises(OIDCError):
        OIDCConfig.from_environment()


def test_blank_optional_compose_values_use_safe_defaults(configuration, monkeypatch):
    for suffix in (
        "ALLOWED_ENDPOINT_ORIGINS",
        "SESSION_SECONDS",
        "TOKEN_AUTH_METHOD",
        "DISPLAY_NAME",
        "GROUPS_CLAIM",
    ):
        monkeypatch.setenv("OPENBRICKS_OIDC_" + suffix, "")
    result = OIDCConfig.from_environment()
    assert (
        result.session_seconds == 3600 and result.auth_method == "client_secret_basic"
    )
    assert result.display_name == "Single sign-on" and result.group_claim == "groups"


def test_removing_all_groups_revokes_old_oidc_sessions_and_managed_memberships(
    configuration, tmp_path
):
    store = Store(tmp_path)
    Access(store).seed()
    claims = {"sub": "subject", "groups": ["fixture-editors"]}
    account = synchronize_identity(store, configuration, claims)
    store.put(
        "sessions",
        {"id": "old-session", "user_id": account["id"], "auth_provider": "oidc"},
    )
    claims["groups"] = []
    with pytest.raises(OIDCError):
        synchronize_identity(store, configuration, claims)
    assert store.get("sessions", "old-session") is None
    assert Access(store).role(account) is None


def test_disposable_browser_provider_fixture_round_trip(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/v5-oidc-provider.py"
    if not path.is_file():
        pytest.skip("Browser provider script is tested from the full checkout")
    spec = importlib.util.spec_from_file_location(
        "openbricks_oidc_browser_fixture_test", path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    server = module.start_server(
        "127.0.0.1", 0, redirect_uri="http://testserver/api/auth/oidc/callback"
    )
    base = f"http://127.0.0.1:{server.server_port}"
    server.oidc_state.issuer = server.oidc_state.public_url = base
    try:
        for suffix, value in {
            "ISSUER": base,
            "CLIENT_ID": "openbricks-smoke",
            "CLIENT_SECRET": "",
            "TOKEN_AUTH_METHOD": "none",
            "REDIRECT_URI": "http://testserver/api/auth/oidc/callback",
            "ALLOW_HTTP": "true",
            "DEV_HTTP_HOSTS": "testserver",
            "GROUP_ROLE_MAP": '{"openbricks-smoke-users":{"default":"editor"}}',
        }.items():
            monkeypatch.setenv("OPENBRICKS_OIDC_" + suffix, value)
        service = OIDCLogin(OIDCConfig.from_environment())
        url, binding = service.begin()
        chooser = httpx.get(url, timeout=5)
        assert chooser.status_code == 200 and "Continue as Alice" in chooser.text
        authorized = httpx.get(
            url + "&identity=alice", follow_redirects=False, timeout=5
        )
        assert authorized.status_code == 302
        args = {
            key: values[0]
            for key, values in parse_qs(
                urlsplit(authorized.headers["location"]).query
            ).items()
        }
        claims, _ = service.complete(args["state"], binding, args["code"])
        assert claims["name"] == "Smoke Alice" and claims["groups"] == [
            "openbricks-smoke-users"
        ]
        assert not server.oidc_state.codes
    finally:
        server.shutdown()
        server.server_close()
        server.oidc_thread.join(timeout=5)
