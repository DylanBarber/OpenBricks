# Projects and access

Projects group notebooks, folders, Git repositories, jobs, workflows, saved queries, and data-operation history. Select a project from the top bar; workspace and history views load that project's accessible resources.

Project permissions are application controls for a **trusted team**. Notebook Python and SQL still execute with the compute account's filesystem, network, database, and storage permissions. Projects do not create an operating-system sandbox, separate Spark sessions, or an isolation boundary for untrusted authors.

## Roles

| Project role | Browse resources and results | Execute saved work | Edit source and definitions | Manage project membership/settings |
| --- | --- | --- | --- | --- |
| Viewer | Yes | No | No | No |
| Operator | Yes | Yes | No | No |
| Editor | Yes | Yes | Yes | No |
| Owner | Yes | Yes | Yes | Yes |

Global workspace administrators have implicit owner access to every project. Their project access cannot be reduced through a project membership row. Administrators also create global accounts, saved connections, and compute registrations and manage backups and notification destinations. Creating projects and assigning their connections/compute remain administrator actions.

The original workspace becomes **Shared workspace**. Existing member accounts are seeded as editors there; newly created member accounts are also added to it. Review that membership when establishing more restricted project access. Removing membership takes away that project's application access; an owner cannot remove their own membership through the API.

## Create a project and invite existing team accounts

1. As an administrator, open **Projects & access → New project**. Enter a name and description.
2. Select the connections and compute targets appropriate for this project, then save.
3. Open the project's **Members**. Choose an existing team account and assign a role.
4. Change or remove memberships there as the team changes. Create new global accounts first through **Team & settings**.

Owners can maintain names, descriptions, and membership. Broadening the project's connection or compute assignments requires an administrator. The shared default project cannot be deleted. Other projects must be emptied of their resources before deletion.

## Select notebook credentials explicitly

Making a connection available to a project does not automatically give it to every notebook:

1. An administrator makes a named connection available through the project's **Settings → Allowed connections**. A connection's API-level `project_ids` assignment can also grant availability; existing connections default to the shared project.
2. Open a notebook's **Notebook settings**, confirm its project, and select only the connections it needs.
3. Save, then run it. Code uses the selected registry, for example `connections.sqlserver("Reporting")` or `connections.s3("Lake")`.

An empty notebook connection selection dispatches **no saved connector credentials**. Selection is constrained by project availability, including for administrators. Current permissions and assignments are rechecked for queued work, reruns, and workflow steps. Changing projects in notebook settings clears the prior connection, folder, and default compute assignments; select appropriate replacements and save.

Management sends selected connection definitions and decrypted credentials to the selected compute service for execution. Credentials are refreshed from current saved definitions, rather than embedded into captured notebook source. Register only compute hosts you trust with those credentials. This reduces unnecessary credential distribution; it does not prevent trusted notebook code from using other permissions available to its process.

Use least-privilege accounts at the underlying SQL Server or object store. A project role does not override the database account's grants. A notebook author with arbitrary code execution can act outside the connection helper using the compute account's existing access.

## Data and compute boundaries

A project's compute list controls which execution targets non-administrators can select through the application. Each target has its own queue and interpreter. Several projects on the same target can share Spark temporary views and visible files; choose distinct names and persistent storage locations.

Local buckets and the local metastore belong to the compute host, not to a project-specific storage sandbox. The optional bundled Unity Catalog is shared metadata with authorization disabled in its private development configuration. Application project checks do not turn it into per-user table governance. Secure shared data at the storage/catalog/database layer when stronger boundaries are needed.

See [SECURITY.md](../SECURITY.md), [workflow execution](WORKFLOWS.md), and [deployment](../DEPLOYMENT.md) for the complete trust and network model.
