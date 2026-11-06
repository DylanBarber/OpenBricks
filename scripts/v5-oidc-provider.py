"""Disposable OIDC provider for OpenBricks smoke tests, never production identity.

All identities and tokens are synthetic. RSA keys stay in process memory.
The smoke harness owns the server and publishes its port on host loopback only.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt


@dataclass
class ProviderState:
    issuer: str
    public_url: str
    redirect_uri: str
    client_id: str = "openbricks-smoke"
    key: object = field(
        default_factory=lambda: rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        ),
        repr=False,
    )
    codes: dict = field(default_factory=dict, repr=False)
    lock: object = field(default_factory=threading.RLock, repr=False)
    requests: dict = field(default_factory=dict)

    def jwk(self):
        return {
            **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key())),
            "kid": "openbricks-smoke-rsa",
            "use": "sig",
            "alg": "RS256",
        }

    def accepted(self, values):
        return (
            values.get("client_id") == self.client_id
            and values.get("redirect_uri") == self.redirect_uri
            and values.get("response_type") == "code"
            and "openid" in values.get("scope", "").split()
            and values.get("code_challenge_method") == "S256"
            and re.fullmatch(r"[A-Za-z0-9_-]{43}", values.get("code_challenge", ""))
            and all(
                re.fullmatch(r"[A-Za-z0-9_-]{20,200}", values.get(key, ""))
                for key in ("state", "nonce")
            )
        )


def start_server(
    bind="0.0.0.0", port=8036, *, issuer=None, public_url=None, redirect_uri=None
):
    """Return the running server; caller owns shutdown/server_close."""
    state = ProviderState(
        issuer or os.getenv("OPENBRICKS_SMOKE_OIDC_ISSUER", "http://receiver:8036"),
        public_url
        or os.getenv("OPENBRICKS_SMOKE_OIDC_PUBLIC_URL", "http://127.0.0.1:8036"),
        redirect_uri
        or os.getenv(
            "OPENBRICKS_SMOKE_OIDC_REDIRECT_URI",
            "http://127.0.0.1:3001/api/auth/oidc/callback",
        ),
        os.getenv("OPENBRICKS_SMOKE_OIDC_CLIENT_ID", "openbricks-smoke"),
    )
    callback = urlsplit(state.redirect_uri)
    if (
        callback.scheme not in ("http", "https")
        or not callback.hostname
        or callback.username
        or callback.password
        or re.search(r"[\s;'\"<>]", state.redirect_uri)
    ):
        raise ValueError("Smoke callback URI is invalid")
    callback_origin = f"{callback.scheme}://{callback.netloc}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_body(self, body, status=200, content_type="application/json"):
            raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                # Chromium also checks redirects after a form submission. Permit
                # only this fixture and its explicitly configured callback origin.
                f"default-src 'none'; form-action 'self' {callback_origin}; style-src 'unsafe-inline'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path)
            with state.lock:
                state.requests[path.path] = state.requests.get(path.path, 0) + 1
            if path.path == "/health":
                return self.send_body({"status": "ok", "synthetic_provider": True})
            if path.path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.path == "/.well-known/openid-configuration":
                return self.send_body(
                    {
                        "issuer": state.issuer,
                        "authorization_endpoint": state.public_url + "/authorize",
                        "token_endpoint": state.issuer + "/token",
                        "jwks_uri": state.issuer + "/jwks",
                        "response_types_supported": ["code"],
                        "subject_types_supported": ["public"],
                        "id_token_signing_alg_values_supported": ["RS256"],
                        "code_challenge_methods_supported": ["S256"],
                        "token_endpoint_auth_methods_supported": ["none"],
                    }
                )
            if path.path == "/jwks":
                return self.send_body({"keys": [state.jwk()]})
            if path.path != "/authorize":
                return self.send_body({"error": "not_implemented"}, 501)
            raw = parse_qs(path.query)
            if any(len(values) != 1 for values in raw.values()):
                return self.send_body({"error": "invalid_request"}, 400)
            values = {key: items[0] for key, items in raw.items()}
            if not state.accepted(values):
                return self.send_body({"error": "invalid_request"}, 400)
            identity = values.get("identity")
            if identity not in ("alice", "bob"):
                fields = "".join(
                    f'<input type="hidden" name="{html.escape(key, quote=True)}" value="{html.escape(value, quote=True)}">'
                    for key, value in values.items()
                    if key != "identity"
                )
                document = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OpenBricks smoke identity</title><style>body{{font:16px system-ui;background:#121a22;color:#e7eef5;display:grid;place-items:center;min-height:95vh}}main{{max-width:460px;padding:32px;border:1px solid #354555;border-radius:16px}}p{{line-height:1.6;color:#b7c6d3}}button{{display:block;width:100%;margin:12px 0;padding:14px;background:#f1b788;border:0;border-radius:8px;font:inherit;cursor:pointer}}</style></head><body><main><h1>OpenBricks test sign-in</h1><p>Choose a synthetic identity for this disposable smoke workspace. These accounts do not grant production access.</p><form method="get" action="/authorize">{fields}<button name="identity" value="alice">Continue as Alice</button><button name="identity" value="bob">Continue as Bob</button></form></main></body></html>"""
                return self.send_body(document, content_type="text/html; charset=utf-8")
            with state.lock:
                instant = time.time()
                state.codes = {
                    key: entry
                    for key, entry in state.codes.items()
                    if entry["expires"] > instant
                }
                if len(state.codes) >= 256:
                    return self.send_body({"error": "temporarily_unavailable"}, 503)
                code = secrets.token_urlsafe(32)
                state.codes[code] = {
                    "challenge": values["code_challenge"],
                    "nonce": values["nonce"],
                    "identity": identity,
                    "expires": instant + 300,
                }
            self.send_response(302)
            self.send_header(
                "Location",
                state.redirect_uri
                + "?"
                + urlencode({"code": code, "state": values["state"]}),
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            if urlsplit(self.path).path != "/token":
                return self.send_body({"error": "not_implemented"}, 501)
            with state.lock:
                state.requests["/token"] = state.requests.get("/token", 0) + 1
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise ValueError()
                raw = parse_qs(self.rfile.read(length).decode())
                if any(len(values) != 1 for values in raw.values()):
                    raise ValueError()
                values = {key: items[0] for key, items in raw.items()}
            except (ValueError, UnicodeError):
                return self.send_body({"error": "invalid_request"}, 400)
            if (
                values.get("grant_type") != "authorization_code"
                or values.get("client_id") != state.client_id
                or values.get("redirect_uri") != state.redirect_uri
            ):
                return self.send_body({"error": "invalid_client"}, 400)
            with state.lock:
                original = state.codes.pop(values.get("code", ""), None)
            verifier = values.get("code_verifier", "")
            actual = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if (
                not original
                or original["expires"] <= time.time()
                or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)
                or not hmac.compare_digest(actual, original["challenge"])
            ):
                return self.send_body({"error": "invalid_grant"}, 400)
            instant = int(time.time())
            claims = {
                "iss": state.issuer,
                "sub": "openbricks-smoke-" + original["identity"],
                "aud": state.client_id,
                "iat": instant,
                "exp": instant + 180,
                "nonce": original["nonce"],
                "name": "Smoke " + original["identity"].title(),
                "preferred_username": original["identity"],
                "groups": ["openbricks-smoke-users"],
            }
            encoded = jwt.encode(
                claims,
                state.key,
                algorithm="RS256",
                headers={"kid": "openbricks-smoke-rsa"},
            )
            return self.send_body(
                {
                    "id_token": encoded,
                    "access_token": secrets.token_urlsafe(32),
                    "token_type": "Bearer",
                    "expires_in": 180,
                }
            )

    server = ThreadingHTTPServer((bind, port), Handler)
    server.oidc_state = state
    server.oidc_thread = threading.Thread(
        target=server.serve_forever, name="smoke-oidc", daemon=True
    )
    server.oidc_thread.start()
    return server


if __name__ == "__main__":
    server = start_server()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()
