# Security

OpenBricks is intended for a **small, trusted team**. A login account is not an isolation boundary for executing untrusted notebooks. Understand the deployment's trust model before opening it to other users.

## Report a vulnerability privately

If the GitHub repository has private vulnerability reporting enabled, use **Security → Report a vulnerability**. Otherwise, ask a repository maintainer for a private reporting channel without posting exploit details, credentials, or affected data in a public issue. No private reporting address or response-time commitment has been established in this source tree.

Include the affected revision, a minimal reproduction using synthetic data, prerequisites, impact, and any suggested fix. Remove secrets and private code from attachments. Ordinary non-sensitive bugs belong in public issues.

## Deployment boundaries

- Members can write and execute Python/Spark code with the selected compute account's permissions, use saved connections, and access that compute host's visible data. There are no per-notebook ACLs, isolated per-user kernels, or enterprise SSO in this release.
- Administrators configure users, saved connections, remote compute, and catalog metadata. Runs receive only explicitly selected named connections that are available to their project; empty selections send none. Operators can run work, editors can change source, and project owners manage membership. These application controls do not make a shared interpreter/filesystem an isolation boundary for untrusted code. Register only compute hosts you trust with the selected credentials.
- Use HTTPS and restrict network access for remote UI and compute endpoints. The private compute API requires a separate long token. Unencrypted HTTP is an explicit option for trusted networks, not a substitute for TLS across an untrusted network.
- The bundled Unity Catalog service has authorization disabled on the private Docker network. Its local SeaweedFS integration uses static storage credentials. It supplies shared metadata and Spark access, not per-user governance or scoped IAM permissions.
- Keep the catalog, compute API, storage administration, and database services off public interfaces unless you deliberately add the appropriate authentication, transport security, and network controls.

## Protect stored credentials and data

Connection credentials and remote tokens are encrypted with the persistent `/data/secrets.key`, or an explicitly configured `OPENBRICKS_SECRET_KEY`. Protect and back up the key together with metadata. Possession of both permits credential recovery. Image exports do not back up data volumes.

Keep `.env`, private SSH keys, Git credentials, volume backups, and production data out of Git. Git URLs containing credentials are rejected, but notebook code or printed output can still disclose any secret available to that code. Use named connections and avoid printing credentials.

Git imports, wheel builds, and installed packages can execute third-party code. Review code and package sources before using them, and apply least-privilege database/storage accounts. The application is not a malware sandbox.

## Supported revisions

OpenBricks is an early project without a published long-term support policy. Include the exact commit when reporting a problem, and check the current source and dependency versions before deploying an older revision. This policy does not claim that any revision has undergone an independent security audit.

See [DEPLOYMENT.md](DEPLOYMENT.md) for network, TLS, persistence, backup, and resource configuration.
