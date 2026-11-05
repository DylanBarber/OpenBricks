# OpenBricks CLI

Deploy and run OpenBricks notebook projects from a terminal or CI without a person's password.

```console
pip install ./cli
openbricks --version
```

Create a service account in the OpenBricks management UI, assign its projects, and issue a scoped expiring token. Set `OPENBRICKS_URL` and `OPENBRICKS_TOKEN` in your shell or CI secret store. The client never saves the token to disk or prints it.

```console
openbricks validate examples/deployable-project/openbricks.yml --target dev
openbricks plan examples/deployable-project/openbricks.yml --target dev
openbricks deploy examples/deployable-project/openbricks.yml --target dev
openbricks run --workflow WORKFLOW_ID --parameters '{"run_date":"2026-11-05"}' --wait
openbricks status RUN_ID
```

`validate` and `plan` contact the target workspace so permissions, references, and pinned artifacts can be checked. `deploy` validates a fresh plan, then applies that exact plan hash. A concurrent target change returns a conflict rather than replacing newer work.

`pack` works offline and embeds local notebook files into a JSON bundle for the UI. Local files must stay within the bundle directory. `promote DEPLOYMENT_ID --target prod` uses the captured deployment source and package manifest, not later workspace edits.

`upload-wheel FILE --project ID` uploads a wheel, and `capture-environment --project ID` returns its immutable manifest. Put that manifest in the bundle's `environment` field. Wheels are supplied explicitly; deployment does not download packages from PyPI.

Use HTTPS remotely. Unencrypted remote HTTP requires `--allow-http` for a trusted network. Localhost HTTP works for development. A failed/cancelled run returns exit status 1; request/validation errors return 2. Timing out while waiting does not cancel a remote run.

See [project deployment guide](../docs/GIT_AND_DEPLOYMENTS.md) and [example project](../examples/deployable-project/).
