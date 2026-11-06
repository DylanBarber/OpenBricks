"""Run the actual OIDC client against the local Postman provider simulation.

Start `postman simulation run postman/simulations/oidc-v5.sim.yaml --port 4612
--no-history --output ndjson` first. All inputs are synthetic; no account needed.
The full system harness separately covers the actual browser login/callback.
"""

from pathlib import Path
import json
import os
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from openbricks.oidc import OIDCConfig, OIDCError, OIDCLogin, synchronize_identity
from openbricks.permissions import Access
from openbricks.store import Store

BASE = "http://127.0.0.1:4612/oidc"


def login(service):
    url, binding = service.begin("/")
    response = httpx.get(url, follow_redirects=False, timeout=10)
    assert response.status_code == 302
    values = {
        key: item[0]
        for key, item in parse_qs(urlsplit(response.headers["location"]).query).items()
    }
    return service.complete(values["state"], binding, values["code"])[0]


def main():
    os.environ.update(
        {
            "OPENBRICKS_OIDC_ISSUER": BASE,
            "OPENBRICKS_OIDC_CLIENT_ID": "openbricks-smoke",
            "OPENBRICKS_OIDC_CLIENT_SECRET": "",
            "OPENBRICKS_OIDC_TOKEN_AUTH_METHOD": "none",
            "OPENBRICKS_OIDC_REDIRECT_URI": "http://127.0.0.1:3009/api/auth/oidc/callback",
            "OPENBRICKS_OIDC_ALLOW_HTTP": "true",
            "OPENBRICKS_OIDC_GROUP_ROLE_MAP": '{"openbricks-smoke-users":{"default":"editor"}}',
            "OPENBRICKS_OIDC_ALLOWED_ENDPOINT_ORIGINS": "[]",
            "OPENBRICKS_OIDC_GROUPS_CLAIM": "groups",
        }
    )
    configuration = OIDCConfig.from_environment()
    service = OIDCLogin(configuration)
    rejected = []
    with tempfile.TemporaryDirectory(
        prefix="openbricks-oidc-simulation-"
    ) as directory, httpx.Client(timeout=10) as control:
        store = Store(Path(directory))
        Access(store).seed()
        control.post(BASE + "/__test/mode", json={"mode": "valid"}).raise_for_status()
        account = synchronize_identity(store, configuration, login(service))
        assert Access(store).role(account) == "editor" and account["role"] == "member"
        for mode in (
            "bad_nonce",
            "bad_signature",
            "bad_audience",
            "expired",
            "bad_groups",
            "provider_error",
        ):
            control.post(BASE + "/__test/mode", json={"mode": mode}).raise_for_status()
            try:
                claims = login(service)
                synchronize_identity(store, configuration, claims)
            except OIDCError:
                rejected.append(mode)
            else:
                raise AssertionError("A simulated provider failure was accepted")
        assert (
            Access(store).role(account) is None
        )  # verified group removal revokes the managed grant
        control.post(BASE + "/__test/mode", json={"mode": "valid"}).raise_for_status()
        control.post(BASE + "/__test/rotate-key").raise_for_status()
        assert (
            synchronize_identity(store, configuration, login(service))["id"]
            == account["id"]
        )
        requests = control.get(BASE + "/__test/requests").json()
        assert requests.get("POST /token", 0) >= 8 and requests.get("GET /jwks", 0) >= 2
        print(
            json.dumps(
                {
                    "passed": True,
                    "successful_logins": 2,
                    "rejected_scenarios": rejected,
                    "key_rotation": True,
                    "dependency_requests": requests,
                }
            )
        )


if __name__ == "__main__":
    main()
