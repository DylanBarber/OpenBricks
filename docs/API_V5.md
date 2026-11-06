# OpenBricks 0.5 API additions

These APIs use the existing session cookie or a scoped service-account token.
Project access is checked on each request. Lists use `limit` and `offset`; errors
return `detail` with an actionable message. This contract accompanies the new
feature tests and the disposable full-system smoke suite.

| Capability | Contract |
| --- | --- |
| Run profiler | `GET /api/runs/{id}/profile?compare_run_id=...`; both runs must be readable and belong to the same notebook. Samples, Spark stages and plans are bounded and may explicitly be unavailable. |
| Dashboards | Project-scoped CRUD `/api/dashboards`; `POST /{id}/refresh` queues execution; `GET /{id}/snapshot?parameters=<JSON>` only reads a previously cached result. A failed refresh preserves the previous successful snapshot. |
| Data assets | `GET /api/assets`, `GET /api/assets/graph` and `GET/PUT /api/assets/{id}`. Graph depth is at most two and node count at most 100. Health is recorded evidence, not a full live scan on each page view. |
| Notebook secrets | `/api/secret-scopes` and `/api/secret-scopes/{id}/keys/{key}` manage metadata and write-only encrypted values. Notebook `/secret-refs` explicitly selects scope/key names. Runtime values are not part of saved source or manifests. |
| OIDC | `GET /api/auth/providers`, `/api/auth/oidc/login`, `/api/auth/oidc/callback`. Optional deployment configuration; authorization-code flow uses PKCE, state, nonce and verified issuer/audience/signature. |
| Audit history | `GET /api/audit?project_id=...&action=...&actor_id=...` and `/api/audit/export`. Project owners see their projects; administrators may inspect all projects. CSV exports are bounded and escape spreadsheet formulas. |

## Collaboration

`GET /api/notebooks/{id}/collaboration?known_version=N` returns enabled state,
presence and public lease owners. It returns notebook source only when its
version differs. Presence heartbeats expire after 45 seconds and are bounded.
The server never returns another client's lease token.

An editor enables live mode with `PUT /collaboration {enabled,version}`. Different
users can edit different cells concurrently. `POST /collaboration/lease
{client_id,cell_id}` acquires a 30-second exclusive cell lease and returns its
token and the current source SHA-256. Heartbeats renew the client's leases.
`PATCH /collaboration/cell {client_id,cell_id,token,base_hash,source}` atomically
saves only that cell. Stale sources and expired or foreign leases return 409.
`POST /collaboration/release` uses the same body to release a finished edit.
Finish cell edits and disable live mode before changing notebook structure,
importing replacement source, moving or deleting the notebook. Management
restart clears ephemeral leases; saved source remains durable.

Comments use paginated `GET/POST /comments` and `PATCH /comments/{id}` with
`text` or root-thread `resolved`. Editors may discuss a notebook; only the author
may replace their text. Replies have one level. Review requests use `GET/POST
/reviews {reviewer_id,message}` and `POST /reviews/{id}/decision
{decision:'approved'|'changes_requested',message}`. Only the assigned reviewer
may decide, and source changes invalidate that review. Reviews are advisory;
they do not silently change run/deployment permissions. Moving a notebook does
not expose the previous project's discussions to its new project.
