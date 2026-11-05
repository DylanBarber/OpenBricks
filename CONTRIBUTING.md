# Contributing to OpenBricks

OpenBricks brings notebooks, Spark SQL, scheduled work, and reusable Python code to infrastructure you control. Contributions that make those workflows clearer, more reliable, and easier to operate are welcome.

## Start with a useful change

For a bug, describe the action, expected result, actual result, and a small reproducible example. Include the OpenBricks revision, host platform, and relevant service versions. Remove credentials, connection strings, private notebook code, and real datasets from logs and screenshots.

For a substantial feature or architectural change, open an issue explaining the user's problem and proposed behavior before starting a large implementation. Small fixes, examples, and documentation improvements can go straight to a pull request. Report vulnerabilities using [SECURITY.md](SECURITY.md).

## Get the project running

The [Docker quickstart](README.md#quick-start) is the closest match to the deployed product. Build and use your own checkout; there is no dependency on a published OpenBricks image or cloud account. Keep your `.env` private.

For local development, install Node.js 22+, Python 3.12, and Java 17. SQL Server connections additionally need a working unixODBC/ODBC installation and Microsoft's ODBC Driver 18. Docker packages these dependencies for you.

Create a Python environment from the project root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
```

On Windows PowerShell, use `py -3.12 -m venv .venv` followed by `.\.venv\Scripts\Activate.ps1`. In a backend terminal, start the development API:

```bash
export OPENBRICKS_ADMIN_USERNAME=admin
export OPENBRICKS_ADMIN_PASSWORD='replace-with-a-unique-development-password'
export DATA_DIR=../.dev-data
cd backend
python -m uvicorn openbricks.main:app --host 127.0.0.1 --port 8000
```

In PowerShell, set these variables with `$env:NAME = 'value'`. Leave `OPENBRICKS_COMPUTE_URL` unset for this development mode: management uses an embedded interpreter. The normal Compose deployment uses a separate compute service. Do not point a development checkout at production data.

In a second terminal:

```bash
cd web
npm ci
npm run dev
```

Visit `http://localhost:3000`. The UI forwards `/api` requests to the backend on port 8000. Use `API_INTERNAL_URL` if you deliberately move the backend. The backend does not automatically load the Compose `.env`; set development variables in its process environment.

## Where changes belong

| Directory | Responsibility |
| --- | --- |
| `backend/openbricks/` | Authenticated management API, document storage, scheduling, runtime, compute protocol, connectors, and catalog integration |
| `backend/tests/` | Backend regression coverage |
| `web/app/`, `web/components/`, `web/lib/` | Next.js UI, theme, typed API calls, and shared controls |
| `docker/`, `compose*.yaml` | Images, startup, catalog configuration, and deployment topology |
| `examples/` | Runnable notebooks, reusable wheel examples, and the notebook-repository starter |
| `docs/` | Focused guides, examples, and public brand assets |
| `scripts/` | Integration checks and repeatable project tooling |

Read any applicable `AGENTS.md` instructions. This repository's Next.js version includes documentation under its installed `next/dist/docs/`; check the installed version's conventions before changing framework behavior.

## Validate the behavior you changed

For UI changes, run these from `web/`:

```bash
npm run typecheck
npm run build
```

For backend changes, activate the Python environment and run from `backend/`:

```bash
python -m pytest -q
```

Run unit tests without deployment-specific `OPENBRICKS_COMPUTE_URL`, catalog, or storage settings inherited from another shell. Otherwise embedded-runtime tests can accidentally call a real service. A clean final-image check is also available:

```bash
docker compose build openbricks compute
docker run --rm --memory=1g --memory-swap=1g --entrypoint python openbricks:local -m pytest -q
```

Use a disposable Compose project and volumes for integration checks. The smoke scripts create notebooks, schedules, packages, and objects. Never use a real workspace for fixture-creating tests. Record what you actually verified and any unavailable external service; a mocked SQL Server test is different from a live database round-trip. [VERIFICATION.md](VERIFICATION.md) describes existing evidence.

For branding changes, follow [the brand guide](docs/BRAND.md). The SVG kit, app favicon, and UI geometry are generated together:

```bash
node scripts/generate-brand.mjs
# After npm ci in web/, also export PNGs:
node scripts/generate-brand.mjs --png
```

## Make the pull request easy to review

- Explain the user-visible problem and resulting behavior.
- Keep the change focused; include documentation and examples when public behavior changes.
- Add regression coverage for consequential behavior and bug fixes. Avoid tests that merely duplicate implementation details.
- Include a screenshot for a visible UI change and the checks you ran.
- Keep secrets, generated runtime data, dependency directories, and personal machine settings out of the diff.

Use plain language and describe limits honestly. A new example should run on its documented prerequisites and use small, redistributable or synthetic data. Avoid undocumented cloud accounts, paid services, and credentials embedded in notebook source.

## License and attribution

By submitting a contribution, you agree that it may be distributed under this project's [Apache License 2.0](LICENSE). Only contribute material you have the right to share. Preserve third-party licenses and attribution. External notebook repositories remain subject to their own licenses; linking to one does not relicense its code.

Keep discussion respectful and practical. Review the work and its effects on users, and give collaborators enough context to reproduce your findings.
