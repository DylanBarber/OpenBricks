# Release process

Pull requests and main-branch pushes run a production web build and the backend suite inside the compute image. The backend job enables the real Java 17 / Spark / Delta checks. Both checks must pass before a tag release publishes images.

1. Update application/image defaults, deployment examples, changelog, and `docs/releases/<version>.md`. Keep management and compute on the same version/build identity.
2. Run the checks and exercise the disposable Docker deployment. Test upgrades against a copy of existing metadata. Back up the deployment before upgrading.
3. Commit the reviewed source, then push an annotated version tag such as `v0.3.0-beta.1`.
4. The Release workflow runs checks, publishes Linux amd64 images to `ghcr.io/<owner>/openbricks:<version>` and `ghcr.io/<owner>/openbricks-compute:<version>`, and creates a GitHub release with portable Docker image archives/checksums and a deployment archive containing the optional catalog configuration. Prerelease tags create prereleases. It never updates an unversioned `latest` tag.
5. Check the Actions result and verify both images can be pulled before announcing availability. New GitHub Container Registry packages may initially be private: set package visibility to public in GitHub package settings for anonymous installation. The workflow has package write permission, not package administration permission.

The tag identifies source; OCI labels identify version and commit. Notebook environment manifests additionally record the runtime build identity and wheel hashes. A rerun requests the captured identity, so deploy matching compute images when replaying old releases. Arbitrary external database state is not part of that snapshot.

`compose.yaml` builds from source. `compose.release.yaml` uses pinned registry images without a local build. Extract the deployment archive into a folder before using it: the optional catalog profile needs the included `docker/catalog` directory. Copy `.env.example` to `.env`, configure credentials, and run `docker compose -f compose.release.yaml up -d`. For another fork, change `OPENBRICKS_RUNTIME_IMAGE` and `OPENBRICKS_COMPUTE_IMAGE`; package names are derived from the release repository owner. Release notes are selected from `docs/releases/<version>.md` by the workflow.

## Install from portable image archives

The public release also carries `openbricks-<version>-runtime-linux-amd64.tar.gz` and `openbricks-<version>-compute-linux-amd64.tar.gz`, with SHA-256 files. These include the images and their versioned tags, so registry credentials are unnecessary. Verify the checksums, then load both archives:

```console
docker load -i openbricks-0.3.0-beta.1-runtime-linux-amd64.tar.gz
docker load -i openbricks-0.3.0-beta.1-compute-linux-amd64.tar.gz
docker compose -f compose.release.yaml up -d --pull never
```

For an offline server, transfer the archives and deployment bundle together. The optional SeaweedFS and Unity Catalog profiles require their own images to be pulled and saved separately; only the two OpenBricks images are in these archives. On a remote compute host, load only the compute archive and use `compose.compute.yaml`.

Publishing uses GitHub's scoped workflow token. No registry password or private key belongs in the repository. See [GitHub's container publishing guide](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images).
