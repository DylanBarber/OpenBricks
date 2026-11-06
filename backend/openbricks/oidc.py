"""Optional OpenID Connect login; local administrators remain independently usable.

Authorization-code flow with PKCE, browser-bound one-use state/nonce, fixed
administrator-configured HTTPS endpoints and RS256 ID-token verification.
Provider tokens, authorization codes and client secrets are never persisted.
"""

from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
import httpx
import jwt

from .permissions import now

COOKIE = "openbricks_session"
STATE_COOKIE = "openbricks_oidc_state"
ROLES = {"viewer": 0, "operator": 1, "editor": 2, "owner": 3}
MAX_RESPONSE = 1024 * 1024


class OIDCError(ValueError):
    """Only static safe messages belong here; never attach a raw provider body."""


def require(condition, message):
    if not condition:
        raise OIDCError(message)


def _origin(value):
    try:
        parts = urlsplit(value)
        port = parts.port
    except (ValueError, TypeError):
        raise OIDCError("Invalid OIDC endpoint URL") from None
    require(
        parts.scheme in ("http", "https")
        and parts.hostname
        and not parts.username
        and not parts.password
        and not parts.fragment,
        "Invalid OIDC endpoint URL",
    )
    require(
        not any(ord(char) < 33 or char == "\\" for char in value),
        "Invalid OIDC endpoint URL",
    )
    return (
        parts.scheme,
        parts.hostname.lower(),
        port or (443 if parts.scheme == "https" else 80),
    )


@dataclass(frozen=True, repr=False)
class OIDCConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    display_name: str
    group_claim: str
    group_roles: dict
    allowed_origins: frozenset
    dev_http_hosts: frozenset
    auth_method: str
    session_seconds: int

    @classmethod
    def from_environment(cls):
        issuer = os.getenv("OPENBRICKS_OIDC_ISSUER", "").strip()
        if not issuer:
            return None
        client_id = os.getenv("OPENBRICKS_OIDC_CLIENT_ID", "").strip()
        redirect = os.getenv("OPENBRICKS_OIDC_REDIRECT_URI", "").strip()
        require(
            client_id and len(client_id) <= 512 and redirect,
            "Configure the OIDC client ID and callback URL",
        )
        _origin(issuer)
        _origin(redirect)
        require(
            not urlsplit(issuer).query, "OIDC issuer must not contain query parameters"
        )
        require(
            urlsplit(redirect).path == "/api/auth/oidc/callback"
            and not urlsplit(redirect).query,
            "OIDC callback URL must end with /api/auth/oidc/callback",
        )
        try:
            roles = json.loads(os.getenv("OPENBRICKS_OIDC_GROUP_ROLE_MAP", "{}"))
            origins = json.loads(
                os.getenv("OPENBRICKS_OIDC_ALLOWED_ENDPOINT_ORIGINS", "").strip()
                or "[]"
            )
            seconds = int(
                os.getenv("OPENBRICKS_OIDC_SESSION_SECONDS", "").strip() or "3600"
            )
        except (ValueError, TypeError):
            raise OIDCError(
                "Invalid OIDC group, origin or session configuration"
            ) from None
        require(
            isinstance(roles, dict) and 0 < len(roles) <= 100,
            "Configure OIDC group-to-project role mappings",
        )
        for group, projects in roles.items():
            require(
                isinstance(group, str)
                and 0 < len(group) <= 200
                and isinstance(projects, dict)
                and 0 < len(projects) <= 100,
                "Invalid OIDC group mapping",
            )
            require(
                all(
                    isinstance(project, str)
                    and 0 < len(project) <= 100
                    and isinstance(role, str)
                    and role in ROLES
                    for project, role in projects.items()
                ),
                "Invalid OIDC project role mapping",
            )
        require(
            isinstance(origins, list)
            and len(origins) <= 10
            and all(isinstance(v, str) for v in origins),
            "OIDC endpoint origins must be a list",
        )
        dev = os.getenv("OPENBRICKS_OIDC_ALLOW_HTTP", "false").lower() == "true"
        dev_hosts = {"localhost", "127.0.0.1", "::1"} if dev else set()
        if dev:
            dev_hosts.update(
                host.strip().lower()
                for host in os.getenv("OPENBRICKS_OIDC_DEV_HTTP_HOSTS", "").split(",")
                if host.strip()
            )
        allowed = {_origin(issuer)} | {_origin(origin) for origin in origins}
        for value in [issuer, redirect, *origins]:
            origin = _origin(value)
            require(
                origin[0] == "https" or origin[1] in dev_hosts,
                "OIDC requires HTTPS; development HTTP hosts must be explicitly enabled",
            )
        require(
            300 <= seconds <= 86400,
            "OIDC session duration must be between 300 and 86400 seconds",
        )
        secret = os.getenv("OPENBRICKS_OIDC_CLIENT_SECRET", "")
        auth = os.getenv("OPENBRICKS_OIDC_TOKEN_AUTH_METHOD", "").strip() or (
            "client_secret_basic" if secret else "none"
        )
        require(
            auth in ("none", "client_secret_basic", "client_secret_post"),
            "Unsupported OIDC token authentication method",
        )
        require(
            (auth == "none" and not secret) or (auth != "none" and bool(secret)),
            "OIDC client secret and token authentication method disagree",
        )
        return cls(
            issuer,
            client_id,
            secret,
            redirect,
            (os.getenv("OPENBRICKS_OIDC_DISPLAY_NAME", "").strip() or "Single sign-on")[
                :120
            ],
            os.getenv("OPENBRICKS_OIDC_GROUPS_CLAIM", "").strip() or "groups",
            roles,
            frozenset(allowed),
            frozenset(dev_hosts),
            auth,
            seconds,
        )

    def endpoint(self, value):
        require(
            isinstance(value, str) and len(value) <= 2048,
            "Provider returned an invalid endpoint",
        )
        origin = _origin(value)
        require(
            origin in self.allowed_origins,
            "Provider endpoint origin is not explicitly allowed",
        )
        require(
            origin[0] == "https" or origin[1] in self.dev_http_hosts,
            "Provider endpoint requires HTTPS",
        )
        return value


def _json_response(client, method, url, **kwargs):
    """Bounded bytes, no redirects; error bodies and credentials stay private."""
    try:
        with client.stream(method, url, follow_redirects=False, **kwargs) as response:
            require(response.status_code == 200, "The SSO provider request failed")
            chunks, count = [], 0
            for chunk in response.iter_bytes():
                count += len(chunk)
                require(count <= MAX_RESPONSE, "The SSO provider response is too large")
                chunks.append(chunk)
        document = json.loads(b"".join(chunks))
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        if isinstance(exc, OIDCError):
            raise
        raise OIDCError("The SSO provider response is unavailable or invalid") from None
    require(isinstance(document, dict), "The SSO provider response must be an object")
    return document


def return_path(value):
    require(
        isinstance(value, str)
        and 0 < len(value) <= 2048
        and value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and not any(ord(char) < 32 for char in value),
        "Use a relative workspace return path",
    )
    parts = urlsplit(value)
    require(
        not parts.scheme and not parts.netloc and not parts.path.startswith("/api/"),
        "Use a workspace return path",
    )
    return value


class OIDCLogin:
    def __init__(self, config, *, client_factory=httpx.Client, clock=time.time):
        self.config, self.client_factory, self.clock = config, client_factory, clock
        self.lock = threading.RLock()
        self.states = OrderedDict()
        self.metadata = None
        self.metadata_until = 0
        self.jwks = None
        self.jwks_until = 0
        self.attempts = OrderedDict()

    def allow_attempt(self, client):
        instant = self.clock()
        with self.lock:
            for key, values in list(self.attempts.items()):
                values[:] = [value for value in values if value > instant - 300]
                if not values:
                    del self.attempts[key]
            values = self.attempts.get(client, [])
            if len(values) >= 30 or (
                client not in self.attempts and len(self.attempts) >= 2048
            ):
                raise HTTPException(
                    429, "Too many SSO attempts; try again in five minutes"
                )
            self.attempts[client] = [*values, instant]

    def _client(self):
        return self.client_factory(
            timeout=10,
            follow_redirects=False,
            headers={"User-Agent": "OpenBricks-OIDC", "Accept": "application/json"},
        )

    def discovery(self):
        with self.lock:
            if self.metadata and self.clock() < self.metadata_until:
                return self.metadata
            cfg = self.config
            with self._client() as client:
                document = _json_response(
                    client,
                    "GET",
                    cfg.endpoint(
                        cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
                    ),
                )
            require(
                document.get("issuer") == cfg.issuer,
                "Provider issuer does not match the configured issuer",
            )
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                cfg.endpoint(document.get(key))
            require(
                "code" in document.get("response_types_supported", []),
                "Provider does not support authorization code login",
            )
            require(
                "RS256" in document.get("id_token_signing_alg_values_supported", []),
                "Provider must support RS256 ID tokens",
            )
            require(
                "S256" in document.get("code_challenge_methods_supported", ["S256"]),
                "Provider must support PKCE S256",
            )
            require(
                cfg.auth_method
                in document.get(
                    "token_endpoint_auth_methods_supported", ["client_secret_basic"]
                ),
                "Provider does not support the configured client authentication method",
            )
            self.metadata, self.metadata_until = document, self.clock() + 300
            return document

    def begin(self, target="/"):
        metadata, cfg = self.discovery(), self.config
        state, binding, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(4))
        instant = self.clock()
        with self.lock:
            for key, value in list(self.states.items()):
                if value["expires"] <= instant:
                    del self.states[key]
            if len(self.states) >= 256:
                raise OIDCError("Too many pending SSO attempts; try again shortly")
            self.states[hashlib.sha256(state.encode()).hexdigest()] = {
                "binding": hashlib.sha256(binding.encode()).hexdigest(),
                "nonce": nonce,
                "verifier": verifier,
                "return_to": return_path(target),
                "issued": instant,
                "expires": instant + 300,
            }
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        query = urlencode(
            {
                "client_id": cfg.client_id,
                "redirect_uri": cfg.redirect_uri,
                "response_type": "code",
                "scope": "openid profile email",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        separator = "&" if urlsplit(metadata["authorization_endpoint"]).query else "?"
        return metadata["authorization_endpoint"] + separator + query, binding

    def _consume(self, state, binding):
        require(
            isinstance(state, str)
            and 20 <= len(state) <= 200
            and isinstance(binding, str)
            and len(binding) <= 200,
            "The SSO login challenge is missing or invalid",
        )
        with self.lock:
            item = self.states.pop(hashlib.sha256(state.encode()).hexdigest(), None)
        require(
            item
            and item["expires"] > self.clock()
            and hmac.compare_digest(
                item["binding"], hashlib.sha256(binding.encode()).hexdigest()
            ),
            "The SSO login challenge expired or does not belong to this browser",
        )
        return item

    def _keys(self, *, refresh=False):
        with self.lock:
            if self.jwks is not None and self.clock() < self.jwks_until and not refresh:
                return self.jwks
            with self._client() as client:
                document = _json_response(client, "GET", self.discovery()["jwks_uri"])
            keys = document.get("keys")
            require(
                isinstance(keys, list) and 0 < len(keys) <= 64,
                "Provider signing keys are invalid",
            )
            result = {}
            for key in keys:
                require(isinstance(key, dict), "Provider signing key is invalid")
                if (
                    key.get("kty") != "RSA"
                    or key.get("use") not in (None, "sig")
                    or key.get("alg") not in (None, "RS256")
                ):
                    continue
                kid = key.get("kid")
                require(
                    isinstance(kid, str) and 0 < len(kid) <= 200 and kid not in result,
                    "Provider signing key IDs must be unique",
                )
                try:
                    parsed = jwt.PyJWK.from_dict(key, algorithm="RS256").key
                except (ValueError, TypeError, jwt.PyJWTError):
                    raise OIDCError("Provider signing key is invalid") from None
                require(
                    parsed.key_size >= 2048,
                    "Provider RSA signing keys must be at least 2048 bits",
                )
                result[kid] = parsed
            require(result, "No supported provider signing keys were found")
            self.jwks, self.jwks_until = result, self.clock() + 300
            return result

    def complete(self, state, binding, code):
        challenge, cfg = self._consume(state, binding), self.config
        require(
            isinstance(code, str) and 0 < len(code) <= 4096,
            "The SSO authorization code is invalid",
        )
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.redirect_uri,
            "code_verifier": challenge["verifier"],
        }
        auth = None
        if cfg.auth_method == "client_secret_basic":
            auth = httpx.BasicAuth(cfg.client_id, cfg.client_secret)
        else:
            data["client_id"] = cfg.client_id
            if cfg.auth_method == "client_secret_post":
                data["client_secret"] = cfg.client_secret
        with self._client() as client:
            tokens = _json_response(
                client, "POST", self.discovery()["token_endpoint"], data=data, auth=auth
            )
        encoded, access = tokens.get("id_token"), tokens.get("access_token")
        require(
            isinstance(encoded, str)
            and 0 < len(encoded) <= 65536
            and isinstance(access, str)
            and 0 < len(access) <= 65536
            and str(tokens.get("token_type", "")).lower() == "bearer",
            "Provider returned an invalid token response",
        )
        try:
            header = jwt.get_unverified_header(encoded)
            require(
                header.get("alg") == "RS256"
                and not header.get("crit")
                and isinstance(header.get("kid"), str)
                and 0 < len(header["kid"]) <= 200,
                "Provider returned an unsupported ID token header",
            )
            keys = self._keys()
            if header["kid"] not in keys:
                # One bounded refresh allows ordinary provider key rotation.
                keys = self._keys(refresh=True)
            require(header["kid"] in keys, "Provider signing key was not found")
            claims = jwt.decode(
                encoded,
                keys[header["kid"]],
                algorithms=["RS256"],
                audience=cfg.client_id,
                issuer=cfg.issuer,
                leeway=30,
                options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
            )
        except (jwt.PyJWTError, TypeError, KeyError, ValueError) as exc:
            if isinstance(exc, OIDCError):
                raise
            raise OIDCError("Provider ID token verification failed") from None
        require(
            isinstance(claims.get("sub"), str) and 0 < len(claims["sub"]) <= 512,
            "Provider subject is invalid",
        )
        require(
            isinstance(claims.get("nonce"), str)
            and claims["nonce"].isascii()
            and hmac.compare_digest(claims["nonce"], challenge["nonce"]),
            "Provider ID token nonce does not match this login",
        )
        require(
            isinstance(claims.get("iat"), (float, int))
            and not isinstance(claims["iat"], bool)
            and challenge["issued"] - 60 <= claims["iat"] <= self.clock() + 30,
            "Provider ID token was not issued for this login",
        )
        require(
            isinstance(claims.get("exp"), (float, int))
            and not isinstance(claims["exp"], bool)
            and claims["exp"] > claims["iat"],
            "Provider ID token expiry is invalid",
        )
        audiences = (
            claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        )
        require(
            (len(audiences) == 1 or claims.get("azp") == cfg.client_id)
            and claims.get("azp", cfg.client_id) == cfg.client_id,
            "Provider ID token authorized party is invalid",
        )
        for field, original in (("at_hash", access), ("c_hash", code)):
            if field in claims:
                expected = (
                    base64.urlsafe_b64encode(
                        hashlib.sha256(original.encode()).digest()[:16]
                    )
                    .rstrip(b"=")
                    .decode()
                )
                require(
                    isinstance(claims[field], str)
                    and claims[field].isascii()
                    and hmac.compare_digest(claims[field], expected),
                    "Provider token hash verification failed",
                )
        groups = claims.get(cfg.group_claim, [])
        require(
            isinstance(groups, list)
            and len(groups) <= 1000
            and all(isinstance(g, str) and len(g) <= 200 for g in groups),
            "Provider group claim must be a bounded list",
        )
        return claims, challenge["return_to"]


def synchronize_identity(store, config, claims):
    """Issuer+subject identify accounts; email/display names never link accounts."""
    desired = {}
    for group in claims.get(config.group_claim, []):
        for project, role in config.group_roles.get(group, {}).items():
            if project not in desired or ROLES[role] > ROLES[desired[project]]:
                desired[project] = role
    identifier = (
        "oidc-"
        + hashlib.sha256(
            json.dumps([config.issuer, claims["sub"]], separators=(",", ":")).encode()
        ).hexdigest()
    )
    with store.lock:
        require(
            all(store.get("projects", project) for project in desired),
            "SSO project mapping refers to a missing project",
        )
        previous = store.get("users", identifier)
        require(
            not previous
            or (
                previous.get("auth_provider") == "oidc"
                and previous.get("role") == "member"
                and previous.get("_oidc_subject") == claims["sub"]
                and previous.get("_oidc_issuer") == config.issuer
                and not previous.get("disabled")
                and not previous.get("is_service_account")
            ),
            "The SSO account is unavailable",
        )
        if not desired:
            # A verified login with all mapped groups removed must also revoke
            # this identity's previously issued sessions and managed grants.
            if previous:
                affected = []
                for member in store.list("project_members"):
                    if (
                        member.get("user_id") == identifier
                        and member.get("source") == "oidc"
                    ):
                        affected.append(member["project_id"])
                        store.delete("project_members", member["id"])
                with store.connect() as db:
                    db.execute(
                        "DELETE FROM documents WHERE kind='sessions' AND json_extract(body,'$.user_id')=? AND json_extract(body,'$.auth_provider')='oidc'",
                        (identifier,),
                    )
                from .permissions import Access

                for project in affected:
                    Access(store).audit(
                        previous, "auth.oidc.access_removed", project, identifier
                    )
            raise OIDCError("Your SSO groups are not assigned to any workspace project")
        user = {
            **(previous or {}),
            "id": identifier,
            "username": "sso-" + identifier[5:25],
            "role": "member",
            "auth_provider": "oidc",
            "password_hash": "!oidc-only",
            "display_name": str(
                claims.get("name") or claims.get("preferred_username") or "SSO member"
            )[:120],
            "_oidc_issuer": config.issuer,
            "_oidc_subject": claims["sub"],
            "created_at": (previous or {}).get("created_at", now()),
            "last_login_at": now(),
        }
        store.put("users", user)
        for member in store.list("project_members"):
            if (
                member.get("user_id") == identifier
                and member.get("source") == "oidc"
                and member["project_id"] not in desired
            ):
                store.delete("project_members", member["id"])
        for project, role in desired.items():
            membership = store.get("project_members", project + ":" + identifier)
            # Explicit administrator-managed grants are never silently erased.
            if membership is None or membership.get("source") == "oidc":
                store.put(
                    "project_members",
                    {
                        "id": project + ":" + identifier,
                        "project_id": project,
                        "user_id": identifier,
                        "role": role,
                        "source": "oidc",
                        "created_at": (membership or {}).get("created_at", now()),
                    },
                )
        from .permissions import Access

        Access(store).audit(user, "auth.oidc.login", next(iter(desired)), identifier)
        return user


class OIDCAccessLogFilter(logging.Filter):
    """Uvicorn's standard access log otherwise retains callback code/state URLs."""

    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path = args[2].split("?", 1)[0]
            if path.startswith(("/api/auth/oidc/", "/auth/oidc/")):
                record.args = (*args[:2], path, *args[3:])
        return True


def register_oidc_routes(app, *, config=None, client_factory=httpx.Client):
    """Call once during app creation; no provider network call until sign-in."""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, OIDCAccessLogFilter) for item in logger.filters):
        logger.addFilter(OIDCAccessLogFilter())
    configuration_error = False
    if config is None:
        try:
            config = OIDCConfig.from_environment()
        except OIDCError:
            configuration_error = True
    service = OIDCLogin(config, client_factory=client_factory) if config else None
    app.state.oidc = service

    @app.get("/api/auth/providers")
    def providers():
        return {
            "providers": (
                [
                    {
                        "id": "oidc",
                        "name": config.display_name,
                        "login_url": "/api/auth/oidc/login",
                    }
                ]
                if service
                else []
            ),
            "oidc_configured": bool(service),
            "oidc_misconfigured": configuration_error,
        }

    @app.get("/api/auth/oidc/login")
    def begin_login(request: Request, return_to: str = "/"):
        if service is None:
            raise HTTPException(503, "Single sign-on is not configured")
        service.allow_attempt(request.client.host if request.client else "local")
        try:
            target, binding = service.begin(return_to)
        except OIDCError:
            raise HTTPException(
                502, "Single sign-on is unavailable; check the provider configuration"
            ) from None
        response = RedirectResponse(target, status_code=302)
        response.set_cookie(
            STATE_COOKIE,
            binding,
            max_age=300,
            httponly=True,
            secure=urlsplit(config.redirect_uri).scheme == "https",
            samesite="lax",
            path="/api/auth/oidc",
        )
        return response

    @app.get("/api/auth/oidc/callback")
    def finish_login(
        request: Request, state: str = "", code: str = "", error: str = ""
    ):
        if service is None:
            raise HTTPException(503, "Single sign-on is not configured")
        try:
            if error:
                service._consume(state, request.cookies.get(STATE_COOKIE, ""))
                raise OIDCError("The SSO provider did not authorize this login")
            claims, target = service.complete(
                state, request.cookies.get(STATE_COOKIE, ""), code
            )
            user = synchronize_identity(request.app.state.store, config, claims)
        except OIDCError:
            response = RedirectResponse("/?sso_error=login_failed", status_code=303)
            response.delete_cookie(STATE_COOKIE, path="/api/auth/oidc")
            return response
        token = secrets.token_urlsafe(48)
        expires = (
            datetime.now(timezone.utc) + timedelta(seconds=config.session_seconds)
        ).isoformat()
        store = request.app.state.store
        with store.lock:
            with store.connect() as db:
                db.execute(
                    "DELETE FROM documents WHERE kind='sessions' AND json_extract(body,'$.expires_at')<?",
                    (now(),),
                )
            # Replace this browser's old local session to avoid fixation.
            old = request.cookies.get(COOKIE)
            if old:
                store.delete("sessions", hashlib.sha256(old.encode()).hexdigest())
            store.put(
                "sessions",
                {
                    "id": hashlib.sha256(token.encode()).hexdigest(),
                    "user_id": user["id"],
                    "expires_at": expires,
                    "auth_provider": "oidc",
                },
            )
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            secure=urlsplit(config.redirect_uri).scheme == "https",
            samesite="strict",
            max_age=config.session_seconds,
            path="/",
        )
        response.delete_cookie(STATE_COOKIE, path="/api/auth/oidc")
        return response
