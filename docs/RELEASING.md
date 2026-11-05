# Release process

Pull requests and main-branch pushes run a production web build, the backend suite inside the compute image, a separate real SQL Server integration job, CLI package checks, and the [disposable whole-system harness](STABLE_TESTING.md). The backend job enables Java 17 / Spark / Delta checks. All checks must pass before a tag release publishes images.

1. Update application/image defaults, deployment examples, changelog, and `docs/releases/<version>.md`. Keep management and compute on the same version/build identity.
2. Run the checks and the whole-system harness, including remote compute, restart recovery, and a fresh-host encrypted restore. Test upgrades against a copy of existing metadata. Back up the deployment before upgrading.
3. Commit the reviewed source, then push an annotated version tag such as `v0.4.0`.
4. The Release workflow runs checks, publishes Linux amd64 images to `ghcr.io/<owner>/openbricks:<version>` and `ghcr.io/<owner>/openbricks-compute:<version>`, and creates a GitHub release with portable Docker image archives/checksums, the installable CLI wheel, and a deployment archive containing documentation, examples, and the optional catalog configuration. Maintainers can enable the optional Docker Hub publication below. Prerelease tags create prereleases. It never updates an unversioned `latest` tag.
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

GHCR publishing uses GitHub's scoped workflow token. No registry password or private key belongs in the repository. See [GitHub's container publishing guide](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images).

## Optional Docker Hub publication

Docker Hub publication is disabled by default, including in forks. In the repository's **Settings → Secrets and variables → Actions**, configure:

| Type | Name | Value |
| --- | --- | --- |
| Variable | `DOCKERHUB_ENABLED` | `true` to publish to both registries; unset or `false` for GHCR only. Other values fail the image job. |
| Variable | `DOCKERHUB_NAMESPACE` | The Docker Hub account or organization that owns the repositories, without `docker.io/` or a repository suffix. |
| Variable | `DOCKERHUB_USERNAME` | The Docker ID used to authenticate; it must have push access to the selected namespace. |
| Secret | `DOCKERHUB_TOKEN` | A Docker Hub access token for that identity. |

For a personal access token, choose **Repo Read & Write** without Delete or Cloud Sandboxes access, set an expiration appropriate for your release process, and store it directly as the repository secret. A PAT's repository permission applies to every repository that account can access; use an organization access token with access to the two selected repositories when narrower organization-owned access is available. Create the repositories before enabling publication, and choose public visibility if anonymous installation is intended. Consult Docker's [token permissions](https://docs.docker.com/security/access-tokens/reference/) and [PAT setup](https://docs.docker.com/security/access-tokens/personal-access-tokens/). Never paste tokens into a chat, commit them, or place them in `.env.example`. A Docker Desktop sign-in on a developer machine does not configure GitHub Actions credentials.

For an enabled release, each runtime/compute target is built once and the same build is pushed to these versioned tags:

```text
ghcr.io/<github-owner>/openbricks:<version>
ghcr.io/<github-owner>/openbricks-compute:<version>
docker.io/<dockerhub-namespace>/openbricks:<version>
docker.io/<dockerhub-namespace>/openbricks-compute:<version>
```

The existing `checks → images → release` dependency chain applies to both registries. Enabled publication fails before registry login/build if namespace, username, or token is missing. A failed login or push fails the image job and blocks the GitHub release. Disabled publication explicitly reports **Docker Hub skipped** in the Actions job summary. The summary lists published tags only after the image push step succeeds. This configuration follows Docker's [multiple-registry publishing pattern](https://docs.docker.com/build/ci/github-actions/push-multi-registries/).

After both Docker Hub repositories have been published and independently verified, deployment needs only image overrides in `.env`; replace `<namespace>` with their confirmed owner:

```dotenv
OPENBRICKS_RUNTIME_IMAGE=docker.io/<namespace>/openbricks:0.4.0
OPENBRICKS_COMPUTE_IMAGE=docker.io/<namespace>/openbricks-compute:0.4.0
```

Use `compose.release.yaml` on the management host and `compose.compute.yaml` on a separate worker host. These files already support the overrides; they keep the same persistent data volumes and memory controls. Use matching versions/build identities and follow the backup and idle-work checks in [deployment guidance](../DEPLOYMENT.md) before replacing a running installation. For a fully pinned deployment, either override may use `docker.io/<namespace>/<image>@sha256:<verified-digest>` instead of its tag.

Enabling this configuration does not retroactively publish an existing Git tag. The released `v0.4.0` tag and its tested source commit remain immutable. To add Docker Hub for that existing release, copy both already-validated GHCR images by their exact digests to the confirmed namespace, preserve the manifests and all referenced content, and verify anonymous Docker Hub access plus matching manifest/config digests and OCI version/source/revision labels. Refuse an existing destination tag with different content. Do not rebuild the images or move the source tag just to add another registry. An identity-preserving registry copy needs publication and deployment checks; it does not change application behavior or require an application version bump. Future version tags use the workflow configuration committed at those tags.

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
