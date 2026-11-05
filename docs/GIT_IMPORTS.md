# Bring a notebook project in through Git

OpenBricks clones a repository on the management server, imports supported notebooks into workspace folders, and builds Python wheels from projects in that checkout. A Git checkout, a workspace notebook, and an installed wheel are separate things: pulling Git updates neither imports notebook changes nor installs a new package by itself.

Use the bundled [notebook starter](../examples/notebook-repo/) for a complete first project. It includes real Spark bronze/silver/gold transformations, a nine-row CSV inside a wheel, five notebooks, SQL, quality assertions, and one notebook suitable for scheduling. It needs no external data or cloud account.

## Choose where your starter repository lives

There is no separate hosted demo repository address to copy. Choose one of these approaches:

| Approach | Repository URL to enter | Build wheel project directory |
| --- | --- | --- |
| Clone the full OpenBricks source repository | `https://github.com/DylanBarber/OpenBricks.git`, branch `main`, or your fork's URL | `examples/notebook-repo` |
| Copy `examples/notebook-repo/` into its own repository | The URL of that repository on your Git host | `.` |

Cloning the entire source repository also imports other recognized notebooks, such as `examples/hello_spark.py`. Copying the starter into its own repository keeps the first project to its five notebooks.

To create a standalone project, first create an **empty** repository in your Git host's UI. Copy the contents of `examples/notebook-repo/`, including `.gitignore`, to a new directory **outside** your OpenBricks checkout. In a terminal inside that new directory:

```sh
git init -b main
git add .
git commit -m "Add OpenBricks notebook starter"
git remote add origin YOUR_REPOSITORY_CLONE_URL
git push -u origin main
```

`YOUR_REPOSITORY_CLONE_URL` is a placeholder: replace it with your actual clone URL. This creates/pushes your starter only when you run these commands. It is not necessary to run `git init` inside the existing OpenBricks repository.

Supported URL shapes include:

```text
https://github.com/YOUR_ACCOUNT/YOUR_REPOSITORY.git
git@github.com:YOUR_ACCOUNT/YOUR_REPOSITORY.git
ssh://git@YOUR_GIT_HOST/YOUR_GROUP/YOUR_REPOSITORY.git
```

All uppercase names in these examples are placeholders. Self-hosted Git servers work if the management service can resolve and reach them. A path such as `C:\projects\notebooks`, `/srv/notebooks`, or a `file://` URL is not accepted by the Git clone API. For a local-only Git host, expose it over SSH or HTTPS; for a single file, use **Import notebook** instead.

## Clone and import in the UI

1. Open the OpenBricks remote UI and sign in.
2. Open **Git repositories**, select **Clone repository**, and enter **Display name**, **Repository URL**, and **Branch**. The default branch field is `main`; it must exist in your repository. Your display name becomes the repository's workspace root folder name.
3. Select **Clone repository**. The card shows the selected branch and checked-out short commit. Files are cloned on the management server into persistent `/data/repositories/<repository-id>`.
4. On the card, select **Import notebooks**. This is a separate action from cloning. Open **Notebooks** and browse the new repository folder. Subdirectories in Git become nested workspace folders.
5. Open a notebook. Underscores in its filename are replaced with spaces for the initial workspace name. The source file `notebooks/00_run_pipeline.py` appears under `<repository>/notebooks` as **00 run pipeline**.

Import is a recursive scan of the whole checkout, with a maximum of 500 recognized notebooks per import. The UI does not currently offer a subdirectory import filter or file picker. Keep very large source repositories split into smaller notebook projects rather than relying on which 500 files happen to be found first.

## Build the starter wheel and run its pipeline

1. Open **Packages → Build wheel**.
2. Choose the cloned repository. Set **Project directory** to `.` for the standalone starter, or `examples/notebook-repo` for the complete source repository.
3. Select **Build & install**. The directory must contain `pyproject.toml` or `setup.py`. Paths are relative to the checkout; absolute paths, traversal, and symlinks are rejected.
4. Confirm that `openbricks-notebook-demo` version `0.1.0` appears in the package list.
5. In **Notebooks**, open **00 run pipeline**, choose the built-in compute service, and select **Run all**. Spark starts when first used, so the first run takes longer.
6. Expect 9 bronze rows, 6 valid silver orders, 2 quarantined orders, and 5 completed orders totaling **85.50**. Run it a second time to verify that the demo replaces outputs without duplicating them.
7. Open **Object storage → openbricks-demo** to view raw CSV and Parquet output files. Use the gold SQL notebook on the same compute service to query `spark_catalog.openbricks_demo.daily_revenue`.

The wheel is the distribution `openbricks-notebook-demo`; import it as `openbricks_demo`. Its CSV is package data, so it is available on remote compute after the wheel synchronizes. Other starter helper `.py` files are ordinary package code and are not discovered as notebooks.

Builds use `python -m build --wheel --no-isolation`. Install uses `pip --no-index --no-deps` into persistent `/data/site-packages`. OpenBricks does not resolve/download your runtime dependencies automatically. Install/upload the necessary compatible dependency wheels first, and use build requirements available in the build environment. The starter uses the runtime's Spark plus preinstalled setuptools/wheel and has no extra dependencies.

Building a project executes its build code on the management server, and notebook execution runs trusted code on the selected compute service. Use repositories and package sources trusted by the team operating that server. Package changes require an idle environment and restart the interpreter; if work is queued/running, finish it first and retry the package change. Save work before installing.

## Schedule the complete notebook

After one successful manual run:

1. Open **Jobs & schedules → Create job**.
2. Name it, search for **00 run pipeline**, and select the intended compute service. Choosing **Notebook default compute** follows that notebook's stored default; selecting a service on the job pins the job to it.
3. In **Basic**, select **Every day**, enter `08:00`, and set an explicit time zone, for example `UTC`. For weekdays, choose **On selected weekdays** and select Monday–Friday.
4. Create the job and use **Run now** to check it. Inspect **Run history**, select the notebook, then select an individual run.
5. **Advanced** accepts a five-field cron expression. `0 8 * * 1-5` runs Monday–Friday at 08:00 in the selected time zone. Use `UTC` to avoid daylight-saving clock changes, or an IANA zone such as `America/New_York` when local wall-clock time is intended.

The starter's `00_run_pipeline.py` imports shared wheel functions and runs all layers in order. Schedule that notebook alone. OpenBricks jobs currently target one notebook; they do not implement notebook dependency DAGs, `%run`, or `dbutils.notebook.run`. A scheduled run executes the current saved workspace notebook, without automatically pulling Git or rebuilding wheels.

The idle timeout releases the interpreter after 15 minutes without work by default. Saved notebooks, wheel files, blobs, Parquet outputs, and the local metastore remain; Python variables and temporary views disappear. Write a scheduled notebook so it imports dependencies and reads its inputs afresh every time, as the starter does.

## Supported source formats and cell boundaries

| Format | Git discovery and import behavior |
| --- | --- |
| Jupyter `.ipynb` | Imports code/Markdown cells, without stored execution outputs. A code cell beginning `%%sql` becomes a SQL cell. |
| Databricks source `.py` | Requires `# Databricks notebook source` within the first 100 characters to be discovered by Git import. Put it on the first line. Split cells with `# COMMAND ----------`. |
| `.sql` | Discovered automatically. Usually one SQL cell/file; Spark executes a single SQL statement per cell. |
| Ordinary Python modules | Not imported as notebooks unless they contain the Databricks marker. Put reusable modules into a wheel. |
| `.dbc`, HTML exports, R, Scala | Not supported notebook import formats. Export as Jupyter or supported Python/SQL source. |

A Databricks source example with all three cell types:

```python
# Databricks notebook source
# MAGIC %md
# MAGIC # My first Git notebook

# COMMAND ----------
rows = spark.createDataFrame([("North", 25), ("South", 20)], ["region", "revenue"])
rows.createOrReplaceTempView("regional_sales")
display(rows)

# COMMAND ----------
# MAGIC %sql
# MAGIC SELECT region, revenue FROM regional_sales ORDER BY revenue DESC
```

`# MAGIC` prefixes are removed during import. `%md`, `%python`, and `%sql` at the beginning of a source cell determine its language. An ordinary `.py` uploaded directly through **Import notebook** is parsed too, but Git discovery intentionally requires the marker so modules are not accidentally imported.

Notebooks must be UTF-8, at most 10 MB, and at most 500 cells. Git scanning skips hidden path components, `node_modules`, `__pycache__`, `venv`, and symlinked files; paths escaping the repository are rejected. `.gitignore` alone does not hide an already tracked SQL/notebook file from the importer.

Do not place arbitrary schema-migration `.sql` scripts in a notebook repository unless you want them imported as notebooks. Use one statement per SQL cell; Databricks `%pip`, widgets, `dbutils.secrets`, DBFS mounts, autoloader, and other Databricks-specific services are not implemented by this importer/runtime.

## Pulling updates and protecting workspace edits

The normal update sequence is:

```text
Edit locally → commit and push → Pull latest → Import notebooks
                                      └────→ Build & install, if package code changed
```

**Pull latest** runs `git pull --ff-only` on the management checkout. It does not import notebooks. **Import notebooks** matches existing workspace notebooks by repository record and source relative path:

| Change | Result after importing again |
| --- | --- |
| Existing source file changed | Replaces that notebook's source, cells, name, and description from Git; keeps its notebook ID and run history. |
| Notebook edited in the browser | Edits stay only in the workspace until another import replaces them. There is no UI commit/push back to Git. Export or copy important UI edits before re-importing. |
| Workspace notebook moved or assigned default compute | Re-import preserves that notebook's current workspace folder and compute assignment. |
| New source path | Creates another workspace notebook in the corresponding repository directory. |
| Source renamed or moved in Git | Treated as a new source path; the old workspace copy remains. Its schedules do not automatically move to the new notebook. |
| Source deleted in Git | Existing workspace copy remains; remove it manually if appropriate. |
| Same URL cloned as another repository card | Creates a separate checkout and separate workspace copies. |
| Package code changed | Existing installed wheel is unchanged until **Build & install** is run again. Bump your package version so changes are identifiable. |

Re-importing updates stable IDs; it does not just create fresh copies on every import. Jobs reference those IDs and execute the current saved cells. Keep development copies and scheduled notebooks organized so an import has the intended effect on future runs.

The **Branch** field is passed to `git clone --single-branch --branch`. A branch is the normal update workflow; Git also accepts a tag there and checks it out detached. Use a new clone/card to select another branch or tag: the UI does not switch an existing checkout. An arbitrary commit SHA is not a supported ref selector. A tag checkout generally has no tracking branch for **Pull latest**, so treat it as a fixed snapshot. This follows [Git's clone/ref behavior](https://git-scm.com/docs/git-clone).

OpenBricks does not initialize submodules recursively, provide Git LFS management, resolve merge conflicts, or offer push/pull-request controls. Git hooks and local `file`/`ext` protocols are disabled for these operations.

## Private repositories with SSH

Git operations run **inside the management container as user 10001**, with `HOME=/data/home`. Keys or Git credentials installed only on your laptop/Windows host are not automatically available there. The compute containers do not need Git credentials to execute imported notebook content and installed wheels.

For a single private repository, a read-only SSH deploy key is a straightforward choice. A GitHub deploy key grants access to one repository and can be left read-only; see [GitHub's deploy-key documentation](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys).

1. Create a dedicated deploy key using your Git host's instructions. Register only its **public** key on the repository and leave write access disabled.
2. Make the private key available to the management container using your deployment's secret mechanism or a read-only bind mount. It must be readable by UID 10001, and OpenSSH must accept its restrictive file permissions. On Linux a private key owned by that UID with mode `0600` or `0400` is appropriate. Do not commit it, bake it into the image, or paste it into the repository URL.
3. Provision a `known_hosts` file with the Git server's verified host key. Compare against a fingerprint supplied by your Git administrator or the host's published fingerprints. For GitHub use [the published SSH fingerprints/host keys](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints). `ssh-keyscan` output alone does not establish trust.
4. Configure Git/SSH to use those files without interactive prompts. The example override below forces host verification and noninteractive operation.
5. Recreate the management service with the override, then clone using the SSH URL. Git cannot display a browser credential prompt, password prompt, private-key passphrase prompt, or first-use host-trust question in OpenBricks.

For example, create a local `compose.git.yaml` beside `compose.yaml`, adjusting host paths to your own secret files:

```yaml
services:
  openbricks:
    environment:
      GIT_SSH_COMMAND: >-
        ssh -i /run/openbricks-git/id_ed25519
        -o IdentitiesOnly=yes
        -o UserKnownHostsFile=/run/openbricks-git/known_hosts
        -o StrictHostKeyChecking=yes
        -o BatchMode=yes
    volumes:
      - type: bind
        source: ./secrets/git
        target: /run/openbricks-git
        read_only: true
```

That host directory must contain your private `id_ed25519` and verified `known_hosts`. Keep `secrets/` out of source control. The directory is an example location, not a path OpenBricks creates or populates for you.

```sh
docker compose -f compose.yaml -f compose.git.yaml up -d openbricks
```

If a passphrase is required, configure an appropriate noninteractive agent/credential mechanism outside the UI. Do not disable host-key checking to make cloning succeed. On Windows Docker bind mounts, verify the permissions presented inside the Linux container; a correctly owned secret or protected file in the persistent volume may be needed if a mounted key is rejected as too permissive.

HTTPS credentials can also be supplied through a noninteractive Git credential helper configured inside the management service. The UI has no per-repository token field. URLs containing a username/password or token are rejected; keep credentials out of URLs, notebook source, and build logs. System Git config is ignored by these operations, so configure service environment/user-level settings rather than relying on `/etc/gitconfig`.

## Packaging and data for remote compute

Before a remote run, OpenBricks sends the notebook content and synchronizes installed wheel artifacts to the selected compute service. It does **not** copy arbitrary checkout files, local data files, host Python environments, or every dependency of your project.

| Resource | Bundled built-in compute | Separate remote compute |
| --- | --- | --- |
| Saved notebook cells | Sent for execution | Sent for execution |
| Wheels installed through Packages | Available in shared persistent data | Transferred and installed before execution |
| Small fixture packaged inside a wheel | Available with package | Available with package |
| Raw cloned repository tree | Shared local volume in the bundled deployment | Not replicated |
| `blob://` data and local Spark metastore | Bundled shared volume | Remote service's own data volume |
| S3-compatible data | Available if endpoint/credentials are configured and reachable | Available if endpoint/credentials are configured and reachable from that service |

Use `importlib.resources` for small fixtures distributed as package data. Use named S3-compatible connections for shared datasets; SeaweedFS can provide S3 locally without a cloud account. A management endpoint like `http://seaweedfs:8333` on the bundled Docker network may be unreachable from a separate remote server, so configure an address reachable by every participating service. `localhost` always means the machine/container doing the connection.

The starter's local Parquet files and `spark_catalog.openbricks_demo.daily_revenue` table are intentionally per compute service. Switching to a separate remote service seeds a separate demo there; it does not reuse the built-in service's files or table. Keep all demo layers on one service. The management UI's local **Object storage** browser shows its own local data, not every remote service's local filesystem.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Clone fails on a repository that opens in your browser | The server/container needs its own network access and credentials; your browser login is not shared with Git. Confirm the actual clone URL and branch. |
| `Host key verification failed` | Provision verified `known_hosts` for the management service. Do not turn verification off. |
| `Permission denied (publickey)` | Key registration, service-user read permissions, identity path, and noninteractive agent/passphrase setup. |
| `Remote branch ... not found` | Enter an existing branch/tag; `main` may instead be `master` or another name. |
| Clone succeeds but no notebooks appear | Select **Import notebooks**. Check supported extensions, `.py` marker, ignored directories, source encoding/size, and the 500-notebook limit. |
| Notebook changed in Git but not the UI | **Pull latest**, then **Import notebooks**. Git and workspace are separate. |
| `ModuleNotFoundError: openbricks_demo` | **Build & install** the starter wheel using the correct project directory before running. Importing notebooks does not install packages. |
| Build backend or dependency missing | Builds do not create an isolated environment; install compatible required wheels or build externally and upload the resulting wheel. |
| Local CSV file works on one service but not another | A checkout/local blob is not copied to remote servers. Package the small file or put shared data in reachable S3 storage. |
| `TABLE_OR_VIEW_NOT_FOUND` in the SQL sample | Run **00 run pipeline** successfully on the same compute service first; the demo uses its local metastore. |
| Expected-row assertions fail after changing input | The assertions describe the supplied fixture. Inspect silver/quarantine and update expectations for your new data. |
| A removed Git notebook is still scheduled | Git deletion does not delete workspace copies or rewrite jobs. Review and remove/move the obsolete workspace notebook/job explicitly. |

## Verify the starter without a Git server

From an OpenBricks source checkout with its Python dependencies installed:

```sh
python examples/notebook-repo/tools/verify_imports.py
```

This uses the actual backend discovery/parser to confirm exactly five starter notebooks and their cell languages/boundaries. It also compiles Python cells and independently checks the sample CSV totals. With Java/PySpark available, add `--execute` to run the imported notebooks through the actual worker and repeat the pipeline in a temporary data directory. It performs no Git operations and leaves the running server's data alone.
