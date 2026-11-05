# Git authoring and project delivery

OpenBricks supports two related workflows: edit and review notebook source in Git, and deploy a complete project definition into a workspace. Git commits remain the source of truth; a deployment records the notebook source, target bindings, and pinned package environment it applied.

## Edit and review Git source

1. Clone your repository under **Git repositories**, then open its Git workspace.
2. Create a feature branch. Branch switching requires a clean working tree.
3. Import notebooks into the workspace and edit their saved source. To add a new workspace notebook, choose **Connect a workspace notebook**, select it, and choose a new `.py`, `.sql`, or `.ipynb` repository path.
4. Select workspace notebooks and choose **Export and stage notebooks**. Open the changed file to inspect its diff against HEAD. You can also edit a checkout file directly with conflict protection or create a new file.
5. Select the files to include, enter a commit message, and commit. Optional author fields let you choose the Git identity. Blank fields use your workspace username and an `openbricks.local` email address.
6. Push the branch. The push does not force-update remote history. If another contributor changed the remote, reconcile that history in a Git client before trying again.
7. Create a pull request. This sends its title, description, branch, and base to GitHub.

### Credentials and remote access

SSH operations use the management server's existing SSH identity/agent and verified host keys. See [Git imports](GIT_IMPORTS.md) for private repository setup. Administrators can configure a repository-scoped GitHub token in the Git workspace for HTTPS pushes and pull-request creation. Its value is encrypted at rest and is never returned by the API. Blank edits preserve the saved token. The current PR integration supports `github.com`; other Git hosts can use their existing SSH/HTTPS credentials and review UI.

### Import conflicts

Re-import checks the current workspace source against its original imported source and the incoming checkout. Workspace edits are preserved by a conflict response. Commit/export those edits first, or explicitly confirm the exact current notebook versions before replacing them. A stale overwrite confirmation is rejected. Importing never moves another project's notebook back into this project.

Comparisons normalize line endings and trailing whitespace. Source files, diffs, branch lists, and workspace summaries have bounded previews. The file editor uses a SHA-256 compare-and-swap check so an older browser cannot overwrite a newer checkout edit. Symlinked paths and Git's internal directory are not editable.

## Automation identities

Workspace administrators create **service accounts**, grant their projects, and issue scoped API tokens. Account grants are viewer, operator, or editor; service accounts cannot be workspace administrators or log in with a password.

- Read tokens can inspect permitted project metadata and source.
- Run tokens include read access and can start work.
- Edit tokens include read/run access and can edit and deploy project resources.

Tokens have a selected project subset and an expiry of 1–365 days. They are displayed once; only their hash and a short display prefix are stored. Keep the full value in a CI secret store. Existing membership is checked on each request, so removing a project grant narrows previously issued tokens too. Revoking a token blocks future API requests.

Scheduled jobs run as the service account using its current project membership. Rotating or expiring the token that created a job does not remove that job. Remove its grant/account or pause the schedule to stop future admissions. Already admitted work may finish. Source/compute/connection permissions are checked again at admission, and targets cannot silently move into a different project.

These permissions organize a trusted team's work. They do not turn arbitrary notebook, package, or Git code into an untrusted-user sandbox.

## Install the CLI

From a source checkout:

```console
pip install ./cli
openbricks --version
```

The release includes an `openbricks_cli-<version>-py3-none-any.whl` for installation on another machine. The CLI only requires Python and its small client dependencies; Spark/Java remain on the server.

Set `OPENBRICKS_URL` and `OPENBRICKS_TOKEN` in your shell or CI secret store. The token is sent as a bearer credential, never as a URL query or a person's password. Use HTTPS remotely. Localhost HTTP works for development; trusted remote HTTP requires the explicit `--allow-http` option.

## Define a project bundle

See the [runnable project example](../examples/deployable-project/). A bundle is YAML or JSON with `format_version: 1`, a stable name, notebook definitions, workflows, schedules, and named targets.

```yaml
format_version: 1
name: daily-orders
targets:
  dev:
    project_id: default
    connections: {source: saved-sql-connection-id}
    clusters: {default: local}
    variables: {region: North}
  prod:
    project_id: production-project-id
    connections: {source: production-sql-connection-id}
    clusters: {default: production-compute-id}
    variables: {region: All}
environment:
  wheels: []
notebooks:
  - key: ingest
    path: notebooks/ingest.py
    connections: [source]
    parameter_definitions:
      - {name: region, type: string, default: "${vars.region}"}
workflows:
  - key: daily
    tasks:
      - {key: ingest, notebook: ingest}
schedules:
  - key: morning
    workflow: daily
    cron: "0 8 * * *"
    timezone: UTC
    enabled: false
```

Paths are relative to the bundle directory. The CLI embeds their UTF-8 source before contacting the server. A file may also contain inline `source` or a `cells` array; use `openbricks pack` to produce the JSON accepted by the deployment UI. Notebook files cannot escape the bundle directory. A bundle supports 500 notebooks, 100 workflows, 200 schedules, and at most 20 MB of definitions/source.

Target connection/compute values are references to existing, permitted workspace resources. Saved passwords never enter a deployment. `${vars.name}` substitutes an entire typed value from target variables; it does not evaluate code or expand arbitrary strings. Task `${params.name}` references retain their existing workflow semantics.

Job/task `resource_options` can set their memory budget and queue priority. Execution still applies the server's project/global limits and uses the authenticated account as owner. The bundle cannot choose another resource-budget owner.

### Pin wheel environments

`environment: {wheels: []}` explicitly selects only the packaged runtime. It does not inherit whichever custom wheels happen to be active on the server.

For custom wheels, upload/build them and capture their environment:

```console
openbricks upload-wheel dist/my_library-1.0.0-py3-none-any.whl --project default
openbricks capture-environment --project default --name daily-orders
```

Put the returned `{id,wheels,runtime}` manifest in the bundle's `environment` field. Every wheel hash is verified against immutable uploaded artifacts. Promotion uses that same manifest; it never substitutes a newer wheel. Other hosts need the same artifacts and compatible runtime images before accepting the deployment.

## Plan, deploy, and promote

```console
openbricks validate examples/deployable-project/openbricks.yml --target dev
openbricks plan examples/deployable-project/openbricks.yml --target dev
openbricks deploy examples/deployable-project/openbricks.yml --target dev
openbricks promote DEPLOYMENT_ID --target prod --plan-only
openbricks promote DEPLOYMENT_ID --target prod
```

Validation checks the target's permissions, connection/compute references, graph, schedules, and pinned artifacts. The plan identifies creates, updates, and unchanged resources. Applying requires the reviewed plan hash; a concurrent resource change requires a fresh plan.

Resource IDs are stable within a project/bundle/key. Repeated deployments update the same resources. A managed notebook moved to another project is protected from being overwritten by its old target. Declared resources are updated together under the automation/workflow/store admission locks; file or schedule-registration failures roll back their metadata/files. Existing resources omitted from a new bundle are preserved, with a plan warning; deployments never prune data automatically.

Promotion reads the bundle captured in the selected deployment. Later edits to workspace notebooks, package choices, or local Git files do not change it. The destination's declared target bindings and current permissions still apply. An actor needs read access to the source deployment and edit access to the destination.

## Run from CI

```console
openbricks run --workflow WORKFLOW_ID --parameters '{"run_date":"2026-11-05"}' --wait
openbricks run --notebook NOTEBOOK_ID --wait
openbricks run --job JOB_ID --wait
openbricks status RUN_ID
openbricks status WORKFLOW_RUN_ID --workflow
```

The client returns status 1 for a failed/cancelled run and 2 for request/validation errors. Waiting can time out without cancelling the server's run; inspect its status before deciding to retry database writes.

The example's [GitHub Actions workflow](../examples/deployable-project/.github/workflows/deploy.yml) uses a self-hosted runner that can reach your private OpenBricks endpoint. Store `OPENBRICKS_URL` and `OPENBRICKS_TOKEN` in repository secrets and give the service account editor access only to intended deployment projects.
