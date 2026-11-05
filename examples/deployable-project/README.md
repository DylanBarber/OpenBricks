# Deployable project example

This project deploys two notebooks, a dependency workflow, and a disabled daily schedule. It needs no extra wheels, external services, or Spark session. Both tasks use the same local compute host and a local bucket; repeat runs overwrite only this example's date-specific JSON fixture.

From the OpenBricks source root, install the CLI with `pip install ./cli`. Create a service account with editor access to the development project, issue a scoped edit token, and configure `OPENBRICKS_URL` / `OPENBRICKS_TOKEN` in your environment.

```console
openbricks validate examples/deployable-project/openbricks.yml --target dev
openbricks plan examples/deployable-project/openbricks.yml --target dev
openbricks deploy examples/deployable-project/openbricks.yml --target dev
```

The returned deployment lists stable notebook/workflow IDs. Open **Workflows** to run `daily`, or use its ID:

```console
openbricks run --workflow WORKFLOW_ID --parameters '{"run_date":"2026-11-05"}' --wait
```

Expected report: `Validated 4 orders and 96.50 revenue for 2026-11-05`. The `deployment-demo` bucket contains `<date>/orders.json`.

For promotion, create a production project and replace `REPLACE_WITH_PRODUCTION_PROJECT_ID` in the bundle before the development deployment. Give the automation identity editor access to both projects and issue a token covering both. Then `openbricks promote DEPLOYMENT_ID --target prod` reuses the captured notebook source and package manifest with the production target's bindings. The schedule stays disabled until you deliberately enable it.

To use the management UI, run `openbricks pack examples/deployable-project/openbricks.yml --output packed-project.json`, then paste that JSON into **Project deployments** and preview/apply its plan.

Copy this folder into your own Git repository to use the sample [CI workflow](.github/workflows/deploy.yml). It runs only on pushes to main or manual dispatch, using a self-hosted runner that can reach your OpenBricks service. Configure its URL/token as repository secrets and update the production project binding.

See [Git and project delivery](../../docs/GIT_AND_DEPLOYMENTS.md) for pinning custom wheels, scoped identities, conflict handling, and API details.
