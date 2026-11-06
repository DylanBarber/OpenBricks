# Notebook secrets and single sign-on

OpenBricks 0.5 adds project-owned notebook secrets and optional OpenID Connect
login. Both reuse the existing workspace accounts, project permissions and
encrypted metadata volume. Neither requires another always-running container.

## Store and select notebook secrets

1. Open **Notebook secrets**, choose a project, and select **New scope**.
2. Give the scope a stable name, such as `warehouse`. Project owners manage
   scopes and keys. Choose which project roles may use its values: operator,
   editor and owner; owners retain access.
3. Add a named key, such as `api_token`. Values are write-only: the management
   API and UI return names, versions and update times, never saved values.
4. Open a notebook and choose **Notebook secrets** in its toolbar. Select the
   exact scope/key pairs it needs and save the selection.
5. Access the selected values in Python:

```python
token = dbutils.secrets.get(scope="warehouse", key="api_token")
# Equivalent helper already available in the notebook namespace:
token = notebook_secrets.get("warehouse", "api_token")

dbutils.secrets.list("warehouse")       # key names only
dbutils.secrets.listScopes()            # selected scope names only
```

Scope and key names contain 1–64 letters, digits, dots, dashes or underscores,
beginning with a letter or digit. A value is limited to 4 KB of UTF-8, a notebook
to 32 selected keys, a scope to 100 keys, and a project to 100 scopes. Names are
case-sensitive. The API supports multiline values; the current password input
is intended for single-line credentials.

Selections contain names, never values. An empty selection dispatches no
notebook-secret values. Project, run-as account and scope-role permissions are
rechecked when a run is dispatched. Rotation resolves the current value for
future dispatches. Deleting a key/scope or removing permission makes a future
dispatch fail before notebook execution. Already-running code may retain a
value it was previously granted; stop that worker when immediate revocation is
needed. Management changes, selections and secret dispatch are audited without
recording values.

## Encryption and captured output

Saved values use the existing authenticated `SecretBox` encryption and
`/data/secrets.key`, or `OPENBRICKS_SECRET_KEY` when configured. Back up encrypted
metadata and its key together. Treat backups as credentials: an unencrypted
archive containing both the metadata and its decryption key can be used to
recover the values. Scheduled off-host backups require archive encryption.

Values travel only in the authenticated management-to-compute execution
envelope and worker memory. They do not belong in notebook source, Git exports,
run snapshots, environment manifests or command-line arguments. Use HTTPS for
remote compute as described in [deployment](../DEPLOYMENT.md).

Known literal values are masked in captured stdout/stderr, errors, table data,
user dictionary keys and profiling strings. Streaming masking handles a value
split across writes. A worker retains a bounded masking history for rotated
values while its Python/JVM session lives; exhausting that budget requires a
fresh worker process so old variables cannot outlive their masking history.
Fixed protocol keys and enum identifiers remain intact even when a short
credential happens to match one.

This is a trusted-code convenience and disclosure safeguard. Notebook authors
can deliberately encode values, send them to other systems or write files;
literal-output masking does not prevent that. Scope permissions do not turn
arbitrary Python into an isolation boundary. Use appropriately restricted
underlying service accounts and trusted compute hosts.

## Secret API

All paths require login and project access. Management requires project owner
access; selection requires editor access; execution requires run access plus
the scope's allowed role.

| Method and path | Behavior |
| --- | --- |
| `GET /api/secret-scopes?project_id=...&limit=25&offset=0` | Paged scope/key metadata, with no values. Maximum page size 100. |
| `POST /api/secret-scopes` | `{name,project_id,description?,allowed_roles?}` creates a scope. |
| `PATCH /api/secret-scopes/{id}` | `{description,allowed_roles}` updates scope policy. Names are immutable. |
| `PUT /api/secret-scopes/{id}/keys/{key}` | `{value}` creates or rotates a key; returns version metadata only. |
| `DELETE /api/secret-scopes/{id}/keys/{key}` | Revokes a key. |
| `DELETE /api/secret-scopes/{id}` | Revokes the scope and its keys. |
| `GET /api/notebooks/{id}/secret-refs` | Notebook selection and current source version, without cell source. |
| `PUT /api/notebooks/{id}/secret-refs` | `{secret_refs:[{scope,key}],version?}` updates explicit selection; stale supplied versions return 409. |

There is intentionally no management endpoint to read a stored value.
Validation errors on these routes do not echo submitted request values.

## Optional OpenID Connect

Configure an OIDC provider that supports authorization-code login, PKCE S256,
RS256 ID tokens, discovery and JWKS. Register this exact callback URL at the
provider, replacing the hostname with the public HTTPS address of your server:

```text
https://openbricks.example.org/api/auth/oidc/callback
```

Example configuration (all values below are placeholders):

```dotenv
OPENBRICKS_OIDC_ISSUER=https://identity.example.org/realms/team
OPENBRICKS_OIDC_CLIENT_ID=openbricks
OPENBRICKS_OIDC_CLIENT_SECRET=configure-outside-git
OPENBRICKS_OIDC_REDIRECT_URI=https://openbricks.example.org/api/auth/oidc/callback
OPENBRICKS_OIDC_DISPLAY_NAME=Team identity
OPENBRICKS_OIDC_GROUP_ROLE_MAP={"data-engineers":{"default":"editor"},"data-readers":{"default":"viewer"}}
```

`GROUP_ROLE_MAP` maps an exact provider group claim to project IDs and roles.
If multiple groups grant the same project, the highest mapped role wins.
Allowed project roles are viewer, operator, editor and owner. The project must
already exist. No provider claim or group mapping grants workspace
administrator access. A verified identity without any mapped group cannot log
in. Removing all mapped groups revokes that identity's managed memberships and
existing OIDC sessions when its next verified login is processed.

| Optional variable | Default / purpose |
| --- | --- |
| `OPENBRICKS_OIDC_GROUPS_CLAIM` | `groups`; use another top-level array claim when your provider emits roles/groups under another name. |
| `OPENBRICKS_OIDC_TOKEN_AUTH_METHOD` | `client_secret_basic` with a client secret, otherwise `none`. `client_secret_post` is also supported. The provider must advertise the selected method. |
| `OPENBRICKS_OIDC_SESSION_SECONDS` | `3600`, bounded from 300 to 86400. |
| `OPENBRICKS_OIDC_ALLOWED_ENDPOINT_ORIGINS` | JSON list, default `[]`. The issuer origin is automatically allowed; explicitly add another origin only when the provider serves authorization/token/JWKS there. |
| `OPENBRICKS_OIDC_ALLOW_HTTP` | `false`; development mock use only. |
| `OPENBRICKS_OIDC_DEV_HTTP_HOSTS` | With HTTP explicitly enabled, extra development hostnames in a comma-separated list. Loopback hosts are already permitted. |

Leaving the issuer unset disables SSO. Partially invalid configuration leaves
local login available and reports SSO as misconfigured. The login page obtains
button metadata from `GET /api/auth/providers`; starting login uses
`GET /api/auth/oidc/login?return_to=/`.

OIDC accounts use the exact issuer and subject as their stable identity.
Email, display name and preferred username never link or take over an existing
local account. Group-managed project memberships are refreshed at verified
login. Explicit manually managed grants are preserved; administrators should
review those separately during offboarding. Session access uses current stored
membership, not a role baked into a browser token. Keep a separate local
administrator account for provider outages and recovery.

The login flow checks signed issuer/audience/authorized-party claims, token
times, nonce, optional token hashes and provider signing keys. Browser-bound
state and PKCE challenges expire after five minutes and are one-use. Signing
key discovery is cached for five minutes with a bounded refresh for key
rotation. HTTP calls use timeouts, bounded response sizes and no redirects;
provider endpoint origins are explicitly confined. OpenBricks does not persist
provider authorization codes, access/refresh tokens or ID tokens. Its Uvicorn
access filter removes OIDC callback/login query strings. Configure external
proxies and development tools to avoid retaining callback query values too.

## Reproduce protocol and browser tests

Backend tests use a real temporary local HTTP provider and generated RSA keys:

```console
cd backend
python -m pytest -q tests/test_oidc_v5.py tests/test_notebook_secrets_v5.py
```

The disposable system smoke harness includes
`scripts/v5-oidc-provider.py` and the Chromium tests in
`web/e2e/security-v5.spec.ts`. They exercise the actual login page, provider
chooser, callback, workspace account, local administrator fallback, secrets UI,
Python worker output masking and rotation. Production data is not used.

For a separate local Postman simulation, return to the repository root.
CLI 1.86.0 was used:

```console
postman simulation run postman/simulations/oidc-v5.sim.yaml --port 4612 --no-history --output ndjson
```

In another terminal with backend dependencies installed:

```console
python scripts/v5-oidc-simulation-check.py
postman simulation stop postman/simulations/oidc-v5.sim.yaml
```

This runs the implemented OIDC client over HTTP against a stateful local
stand-in: two valid logins, six denial/outage scenarios and key rotation.
The simulation log must contain actual `oidc` member discovery, authorization,
token and JWKS requests. It proves the modeled protocol behavior, not a live
Entra, Keycloak or other provider deployment. No real provider credentials or
Postman cloud account are required; owner-workspace mock discovery was skipped
because the CLI was signed out. Nothing is published to a Postman workspace.
