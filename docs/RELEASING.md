# Release process

Pull requests and main-branch pushes run a production web build, the backend suite inside the compute image, a separate real SQL Server integration job, CLI package checks, and the [disposable whole-system harness](STABLE_TESTING.md). The backend job enables Java 17 / Spark / Delta checks. All checks must pass before a tag release publishes images.

1. Update application/image defaults, deployment examples, changelog, and `docs/releases/<version>.md`. Keep management and compute on the same version/build identity.
2. Run the checks and the whole-system harness, including remote compute, restart recovery, and a fresh-host encrypted restore. Test upgrades against a copy of existing metadata. Back up the deployment before upgrading.
3. Commit the reviewed source, then push an annotated version tag such as `v0.4.0`.
4. The Release workflow runs checks, publishes Linux amd64 images to `ghcr.io/<owner>/openbricks:<version>` and `ghcr.io/<owner>/openbricks-compute:<version>`, and creates a GitHub release with portable Docker image archives/checksums, the installable CLI wheel, and a deployment archive containing documentation, examples, and the optional catalog configuration. Prerelease tags create prereleases. It never updates an unversioned `latest` tag.
5. Check the Actions result and verify both images can be pulled before announcing availability. New GitHub Container Registry packages may initially be private: set package visibility to public in GitHub package settings for anonymous installation. The workflow has package write permission, not package administration permission.

The tag identifies source; OCI labels identify version and commit. Notebook environment manifests additionally record the runtime build identity and wheel hashes. A rerun requests the captured identity, so deploy matching compute images when replaying old releases. Arbitrary external database state is not part of that snapshot.

`compose.yaml` builds from source. `compose.release.yaml` uses pinned registry images without a local build. Extract the deployment archive into a folder before using it: the optional catalog profile needs the included `docker/catalog` directory. Copy `.env.example` to `.env`, configure credentials, and run `docker compose -f compose.release.yaml up -d`. For another fork, change `OPENBRICKS_RUNTIME_IMAGE` and `OPENBRICKS_COMPUTE_IMAGE`; package names are derived from the release repository owner. Release notes are selected from `docs/releases/<version>.md` by the workflow.

## Install from portable image archives

The public release also carries `openbricks-<version>-runtime-linux-amd64.tar.gz` and `openbricks-<version>-compute-linux-amd64.tar.gz`, with SHA-256 files. These include the images and their versioned tags, so registry credentials are unnecessary. Verify the checksums, then load both archives:

```console
docker load -i openbricks-0.4.0-runtime-linux-amd64.tar.gz
docker load -i openbricks-0.4.0-compute-linux-amd64.tar.gz
docker compose -f compose.release.yaml up -d --pull never
```

For an offline server, transfer the archives and deployment bundle together. The optional SeaweedFS and Unity Catalog profiles require their own images to be pulled and saved separately; only the two OpenBricks images are in these archives. On a remote compute host, load only the compute archive and use `compose.compute.yaml`.

Publishing uses GitHub's scoped workflow token. No registry password or private key belongs in the repository. See [GitHub's container publishing guide](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images).

## Reproduce the SQL Server integration job

Run contributor checks from a full Git checkout; the deployment bundle contains deployment assets rather than the application build sources. For the complete application proof, use [the system harness](STABLE_TESTING.md).

`compose.sql-tests.yaml` starts a disposable SQL Server Developer container and a compute test container on a private Compose network. The Microsoft SQL Server 2025 image is pinned by digest. Tests create a uniquely named database, exercise ODBC inserts/rollback, Spark JDBC reads, and change-tracking snapshot/update/delete/replay behavior, then drop that database. No production data volume or host SQL port is used.

The harness caps SQL Server at 2.5 GB and the test worker at 3 GB, with swap disabled for both. Run it only when the host has room for these additional containers. It is independent of the normal OpenBricks deployment and starts only when explicitly requested.

In PowerShell 7, choose a strong disposable password and run:

```powershell
$env:OPENBRICKS_SQLSERVER_PASSWORD = Read-Host 'Disposable SQL Server password' -MaskInput
docker compose -f compose.sql-tests.yaml build tests
docker compose -f compose.sql-tests.yaml up --abort-on-container-exit --exit-code-from tests
docker compose -f compose.sql-tests.yaml down --volumes --remove-orphans
Remove-Item Env:OPENBRICKS_SQLSERVER_PASSWORD
```

CI generates and masks a new password for each run and always removes the isolated test project afterward. The normal backend suite skips these live database checks unless `OPENBRICKS_TEST_SQLSERVER=1` is explicitly set.
