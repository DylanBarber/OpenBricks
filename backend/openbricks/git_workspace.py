"""Bounded Git authoring, optimistic file edits, and conflict-aware imports."""

from __future__ import annotations

import hashlib
import base64
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .credentials import SecretBox
from .notebooks import export_notebook, parse_notebook
from .permissions import now

MAX_OUTPUT = 2 * 1024 * 1024


def git_call(base, *args, allowed=(0,), limit=MAX_OUTPUT, timeout=60, extra_env=None):
    """Git output is spooled to disk; neither diffs nor errors grow API memory."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(
                [
                    "git",
                    "--no-replace-objects",
                    "-c",
                    "core.hooksPath=",
                    "-c",
                    "core.fsmonitor=false",
                    "-C",
                    str(base),
                    *args,
                ],
                stdout=output,
                stderr=errors,
                timeout=timeout,
                env={
                    **os.environ,
                    "GIT_TERMINAL_PROMPT": "0",
                    "GCM_INTERACTIVE": "never",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    **(extra_env or {}),
                },
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(
                408, "Git operation timed out; check remote connectivity"
            ) from exc
        if result.returncode not in allowed:
            raise HTTPException(
                409,
                "Git operation could not complete. Check the branch, remote credentials, and uncommitted changes",
            )
        output.seek(0)
        value = output.read(limit + 1)
    return value[:limit], len(value) > limit


def git_text(base, *args, **kwargs):
    return git_call(base, *args, **kwargs)[0].decode("utf-8", errors="replace").strip()


def safe_path(base, value, *, allow_missing=False):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1000
        or "\\" in value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError("Choose a valid relative repository path")
    parts = value.split("/")
    if (
        PurePosixPath(value).is_absolute()
        or any(
            part in ("", ".", "..") or ":" in part or part.endswith((".", " "))
            for part in parts
        )
        or parts[0].lower() == ".git"
    ):
        raise ValueError("Repository paths must stay inside the working tree")
    base = Path(base).resolve()
    path = base.joinpath(*parts)
    current = base
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlinked repository paths are not editable")
    if not path.resolve().is_relative_to(base) or (
        not allow_missing and not path.is_file()
    ):
        raise ValueError("Repository file was not found")
    if path.exists() and not path.is_file():
        raise ValueError("Choose a regular repository file")
    return path


def source_fingerprint(notebook):
    cells = [
        (cell["language"], cell["source"].replace("\r\n", "\n").rstrip())
        for cell in notebook.get("cells", [])
    ]
    return hashlib.sha256(
        json.dumps(cells, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _baseline(base, notebook):
    if notebook.get("_git_source_sha256"):
        return notebook["_git_source_sha256"]
    commit, path = notebook.get("imported_commit"), notebook.get("path")
    if commit and re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit) and path:
        try:
            content, truncated = git_call(
                base, "show", f"{commit}:{path}", limit=10 * 1024 * 1024
            )
            if not truncated:
                return source_fingerprint(parse_notebook(path, content))
        except (HTTPException, ValueError, UnicodeError):
            pass
    return None


def check_import_conflicts(
    store, repository, paths=None, overwrite=False, expected_versions=None
):
    """Reject changed workspace copies unless the caller confirms exact versions."""
    base = store.directory / "repositories" / repository["id"]
    project_id = repository.get("project_id") or "default"
    expected_versions = expected_versions or {}
    conflicts = []
    with store.connect() as db:
        ids = [
            row[0]
            for row in db.execute(
                "SELECT id FROM documents WHERE kind='notebooks' AND COALESCE(project_id,'default')=? AND json_extract(body,'$.repository_id')=? LIMIT 1001",
                (project_id, repository["id"]),
            )
        ]
    if len(ids) > 1000:
        raise HTTPException(
            409,
            "This repository has too many workspace copies to import in one operation",
        )
    for identifier in ids:
        notebook = store.get("notebooks", identifier)
        relative = notebook.get("path")
        if not relative or (paths is not None and relative not in paths):
            continue
        try:
            incoming = safe_path(base, relative)
            if incoming.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("Notebook exceeds the import allowance")
            incoming_hash = source_fingerprint(
                parse_notebook(relative, incoming.read_bytes())
            )
        except ValueError:
            continue  # The normal importer validates eligible incoming files.
        current_hash = source_fingerprint(notebook)
        baseline = _baseline(base, notebook)
        if current_hash == incoming_hash or (
            baseline is not None and current_hash == baseline
        ):
            continue
        if overwrite and expected_versions.get(identifier) == notebook.get(
            "version", 1
        ):
            continue
        conflicts.append(
            {
                "id": identifier,
                "path": relative,
                "name": notebook.get("name"),
                "version": notebook.get("version", 1),
                "reason": (
                    "Workspace source changed since import"
                    if baseline
                    else "No original source snapshot is available"
                ),
            }
        )
    if conflicts:
        raise HTTPException(
            409,
            {
                "message": "Import would overwrite workspace edits. Export or commit them, or explicitly confirm the listed notebook versions",
                "conflicts": conflicts,
            },
        )
    return {"ok": True}


def source_bytes(notebook, filename):
    suffix = Path(filename).suffix.lower()
    if suffix == ".ipynb":
        return (
            json.dumps(export_notebook(notebook), indent=2, ensure_ascii=False) + "\n"
        ).encode()
    if suffix not in (".py", ".sql"):
        raise ValueError("Workspace export supports .py, .sql, and .ipynb")
    chunks = []
    for cell in notebook.get("cells", []):
        source, language = cell["source"], cell["language"]
        if suffix == ".py":
            if language != "python":
                source = "\n".join(
                    "# MAGIC " + line
                    for line in (
                        "%"
                        + ("md" if language == "markdown" else language)
                        + "\n"
                        + source
                    ).splitlines()
                )
        elif language != "sql":
            raise ValueError(
                "A .sql export can contain only SQL cells; use .ipynb for mixed notebooks"
            )
        chunks.append(source.rstrip())
    prefix = "# Databricks notebook source\n" if suffix == ".py" else ""
    return (prefix + "\n\n# COMMAND ----------\n\n".join(chunks) + "\n").encode()


def branch_name(base, name):
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 200
        or name.startswith("-")
        or "\n" in name
        or "\r" in name
    ):
        raise ValueError("Choose a valid branch name")
    git_call(base, "check-ref-format", "--branch", name)
    return name


def repository_status(store, repository):
    base = store.directory / "repositories" / repository["id"]
    raw, truncated = git_call(
        base, "status", "--porcelain=v1", "-z", "--untracked-files=normal"
    )
    records = raw.decode("utf-8", errors="replace").split("\0")
    changes, index = [], 0
    while index < len(records) and records[index] and len(changes) < 500:
        row = records[index]
        status, path = row[:2], row[3:]
        if "R" in status or "C" in status:
            index += 1  # porcelain -z follows renamed destination with source.
        changes.append({"path": path, "status": status})
        index += 1
    branch = git_text(
        base, "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1)
    )
    branches = git_text(
        base, "for-each-ref", "--format=%(refname:short)", "refs/heads/", limit=30_000
    ).splitlines()[:200]
    with store.connect() as db:
        identifiers = [
            row[0]
            for row in db.execute(
                "SELECT id FROM documents WHERE kind='notebooks' AND COALESCE(project_id,'default')=? AND json_extract(body,'$.repository_id')=? LIMIT 501",
                (repository.get("project_id") or "default", repository["id"]),
            )
        ]
    workspace_changes = []
    for identifier in identifiers[:500]:
        notebook = store.get("notebooks", identifier)
        workspace_changes.append(
            {
                "id": identifier,
                "name": notebook["name"],
                "path": notebook.get("path"),
                "version": notebook.get("version", 1),
                "modified": source_fingerprint(notebook) != _baseline(base, notebook),
            }
        )
    return {
        "branch": branch or None,
        "commit": git_text(base, "rev-parse", "HEAD"),
        "branches": branches,
        "changes": changes,
        "workspace_changes": workspace_changes,
        "truncated": truncated or len(changes) >= 500 or len(identifiers) > 500,
        "has_github_token": bool(repository.get("_github_token")),
    }


class FileInput(BaseModel):
    path: str
    source: str = Field(max_length=1_000_000)
    expected_sha256: str | None = None


class BranchInput(BaseModel):
    name: str
    base: str | None = None
    create: bool = True


class StageInput(BaseModel):
    notebook_ids: list[str] = Field(min_length=1, max_length=100)
    versions: dict[str, int] = Field(default_factory=dict)
    overwrite_worktree: bool = False


class LinkNotebookInput(BaseModel):
    notebook_id: str
    path: str
    version: int = Field(ge=1)


class CommitInput(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    paths: list[str] = Field(min_length=1, max_length=200)
    author_name: str | None = Field(default=None, max_length=100)
    author_email: str | None = Field(default=None, max_length=200)


class PushInput(BaseModel):
    branch: str | None = None
    set_upstream: bool = True


class PullRequestInput(BaseModel):
    title: str = Field(min_length=1, max_length=250)
    body: str = Field(default="", max_length=30_000)
    base: str = "main"
    draft: bool = False


class GithubCredentialInput(BaseModel):
    token: str = Field(default="", max_length=4096)


def github_repository(url):
    match = re.fullmatch(
        r"git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?", url
    )
    if match:
        return match[1], match[2]
    parsed = urlsplit(url)
    if (
        parsed.hostname != "github.com"
        or parsed.scheme not in ("https", "ssh")
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Pull requests currently require a github.com repository")
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    if len(parts) != 2 or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts
    ):
        raise ValueError("Choose a github.com owner/repository URL")
    return tuple(parts)


def register_git_routes(app, authenticated, admin, access):
    def lookup(request, identifier, user, action="read"):
        store = request.app.state.store
        repository = store.get("repositories", identifier)
        if not repository:
            raise HTTPException(404, "Repository not found")
        access.require_project(user, repository.get("project_id") or "default", action)
        base = store.directory / "repositories" / identifier
        if not base.is_dir():
            raise HTTPException(404, "Repository checkout is unavailable")
        return store, repository, base

    @app.get("/api/repositories/{id}/git/status")
    def status(id: str, request: Request, user=Depends(authenticated)):
        store, repository, _ = lookup(request, id, user)
        return repository_status(store, repository)

    @app.get("/api/repositories/{id}/git/diff")
    def diff(
        id: str,
        request: Request,
        path: str = Query(max_length=1000),
        user=Depends(authenticated),
    ):
        _, _, base = lookup(request, id, user)
        safe_path(base, path, allow_missing=True)
        value, truncated = git_call(
            base, "diff", "HEAD", "--no-ext-diff", "--no-textconv", "--", path
        )
        return {"text": value.decode("utf-8", errors="replace"), "truncated": truncated}

    @app.get("/api/repositories/{id}/git/file")
    def file(
        id: str,
        request: Request,
        path: str = Query(max_length=1000),
        user=Depends(authenticated),
    ):
        _, _, base = lookup(request, id, user)
        target = safe_path(base, path)
        if target.stat().st_size > 1_000_000:
            raise ValueError("The editor supports files up to 1 MB")
        content = target.read_bytes()
        return {
            "path": path,
            "source": content.decode("utf-8"),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    @app.put("/api/repositories/{id}/git/file")
    def update_file(
        id: str, body: FileInput, request: Request, user=Depends(authenticated)
    ):
        store, repository, base = lookup(request, id, user, "edit")
        with store.lock:
            target = safe_path(base, body.path, allow_missing=True)
            old = target.read_bytes() if target.exists() else b""
            digest = hashlib.sha256(old).hexdigest()
            if body.expected_sha256 != digest:
                raise HTTPException(
                    409,
                    "File changed since it was opened. Reload the diff before saving",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + "." + uuid4().hex + ".edit")
            try:
                temporary.write_text(body.source, encoding="utf-8", newline="\n")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return {
            "path": body.path,
            "sha256": hashlib.sha256(body.source.encode()).hexdigest(),
        }

    @app.post("/api/repositories/{id}/git/branch")
    def branch(
        id: str, body: BranchInput, request: Request, user=Depends(authenticated)
    ):
        store, repository, base = lookup(request, id, user, "edit")
        with store.lock:
            name = branch_name(base, body.name)
            if git_text(base, "status", "--porcelain"):
                raise HTTPException(
                    409,
                    "Commit or resolve working-tree changes before switching branches",
                )
            args = ["switch"]
            if body.create:
                args.extend(["-c", name])
                if body.base:
                    args.append(branch_name(base, body.base))
            else:
                args.append(name)
            git_call(base, *args)
            repository.update(
                branch=name,
                commit_full=git_text(base, "rev-parse", "HEAD"),
                updated_at=now(),
            )
            repository["commit"] = repository["commit_full"][:7]
            store.put("repositories", repository)
        return repository_status(store, repository)

    @app.post("/api/repositories/{id}/git/stage-notebooks")
    def stage(id: str, body: StageInput, request: Request, user=Depends(authenticated)):
        store, repository, base = lookup(request, id, user, "edit")
        with store.lock:
            writes = []
            for identifier in dict.fromkeys(body.notebook_ids):
                notebook = store.get("notebooks", identifier)
                if (
                    not notebook
                    or notebook.get("repository_id") != id
                    or (notebook.get("project_id") or "default")
                    != (repository.get("project_id") or "default")
                ):
                    raise HTTPException(
                        404, "Notebook was not found in this project's repository"
                    )
                if body.versions.get(identifier) != notebook.get("version", 1):
                    raise HTTPException(
                        409,
                        "Notebook changed since it was selected. Refresh before staging",
                    )
                path = safe_path(base, notebook["path"], allow_missing=True)
                if path.exists():
                    if path.stat().st_size > 10 * 1024 * 1024:
                        raise ValueError("Repository notebook is too large")
                    disk = source_fingerprint(
                        parse_notebook(notebook["path"], path.read_bytes())
                    )
                    baseline = _baseline(base, notebook)
                    if (
                        disk not in (baseline, source_fingerprint(notebook))
                        and not body.overwrite_worktree
                    ):
                        raise HTTPException(
                            409,
                            "The working tree also changed. Review its diff before replacing it with workspace source",
                        )
                writes.append((path, source_bytes(notebook, notebook["path"])))
            for path, content in writes:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + "." + uuid4().hex + ".export")
                try:
                    temporary.write_bytes(content)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            git_call(
                base,
                "add",
                "--",
                *[str(path.relative_to(base)).replace("\\", "/") for path, _ in writes],
            )
        return repository_status(store, repository)

    @app.post("/api/repositories/{id}/git/link-notebook")
    def link_notebook(
        id: str, body: LinkNotebookInput, request: Request, user=Depends(authenticated)
    ):
        store, repository, base = lookup(request, id, user, "edit")
        with store.lock:
            notebook = store.get("notebooks", body.notebook_id)
            if not notebook or (notebook.get("project_id") or "default") != (
                repository.get("project_id") or "default"
            ):
                raise HTTPException(
                    404, "Notebook was not found in this project's workspace"
                )
            if notebook.get("repository_id") or notebook.get("bundle_owner"):
                raise HTTPException(
                    409,
                    "This notebook is already managed by Git or a deployment; export a separate copy first",
                )
            if notebook.get("version", 1) != body.version:
                raise HTTPException(
                    409,
                    "Notebook changed before it could be connected. Refresh and try again",
                )
            target = safe_path(base, body.path, allow_missing=True)
            source_bytes(
                notebook, body.path
            )  # Validate the selected interchange format.
            if target.exists():
                raise HTTPException(
                    409, "That repository path exists. Import it or choose a new path"
                )
            notebook.update(
                repository_id=id,
                path=body.path,
                version=body.version + 1,
                updated_at=now(),
            )
            notebook.pop("_git_source_sha256", None)
            notebook.pop("imported_commit", None)
            store.put("notebooks", notebook)
        return {"id": notebook["id"], "path": body.path, "version": notebook["version"]}

    @app.post("/api/repositories/{id}/git/commit")
    def commit(
        id: str, body: CommitInput, request: Request, user=Depends(authenticated)
    ):
        store, repository, base = lookup(request, id, user, "edit")
        with store.lock:
            paths = list(dict.fromkeys(body.paths))
            for path in paths:
                safe_path(base, path, allow_missing=True)
            staged = set(
                filter(
                    None,
                    git_call(base, "diff", "--cached", "--name-only", "-z")[0]
                    .decode()
                    .split("\0"),
                )
            )
            if staged - set(paths):
                raise HTTPException(
                    409, "Select all staged files or unstage them before committing"
                )
            if git_text(base, "diff", "--name-only", "--diff-filter=U"):
                raise HTTPException(
                    409, "Resolve Git merge conflicts before committing"
                )
            name = body.author_name or user["username"]
            email = body.author_email or f"{user['username']}@openbricks.local"
            if any(char in name + email for char in "\r\n\x00") or not re.fullmatch(
                r"[^\s@<>]+@[^\s@<>]+", email
            ):
                raise ValueError("Choose a valid commit author name and email")
            git_call(base, "add", "--", *paths)
            git_call(
                base,
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={email}",
                "commit",
                "-m",
                body.message,
                "--only",
                "--",
                *paths,
            )
            repository.update(
                commit_full=git_text(base, "rev-parse", "HEAD"), updated_at=now()
            )
            repository["commit"] = repository["commit_full"][:7]
            store.put("repositories", repository)
            for item in repository_status(store, repository)["workspace_changes"]:
                notebook = store.get("notebooks", item["id"])
                if notebook["path"] not in paths:
                    continue
                try:
                    if not safe_path(
                        base, notebook["path"], allow_missing=True
                    ).is_file():
                        continue
                    committed_source, truncated = git_call(
                        base, "show", f"HEAD:{notebook['path']}", limit=10 * 1024 * 1024
                    )
                    if truncated:
                        continue
                    committed = parse_notebook(notebook["path"], committed_source)
                    if source_fingerprint(committed) == source_fingerprint(notebook):
                        notebook.update(
                            imported_commit=repository["commit_full"],
                            _git_source_sha256=source_fingerprint(notebook),
                        )
                        store.put("notebooks", notebook)
                except (ValueError, UnicodeError):
                    continue
        access.audit(user, "git.commit", repository.get("project_id"), id)
        return repository_status(store, repository)

    @app.post("/api/repositories/{id}/git/push")
    def push(id: str, body: PushInput, request: Request, user=Depends(authenticated)):
        store, repository, base = lookup(request, id, user, "edit")
        with store.lock:
            current = git_text(base, "symbolic-ref", "--quiet", "--short", "HEAD")
            selected = branch_name(base, body.branch or current)
            if selected != current:
                raise ValueError("Switch to the selected branch before pushing it")
            remote = git_text(base, "remote", "get-url", "origin")
            if remote != repository.get("url"):
                raise HTTPException(
                    409,
                    "The origin URL changed outside OpenBricks. Re-register the repository before pushing",
                )
            args = ["push", "--porcelain"]
            if body.set_upstream:
                args.append("--set-upstream")
            environment = {}
            if repository.get("_github_token") and repository.get("url", "").startswith(
                "https://github.com/"
            ):
                token = SecretBox(store.directory).decrypt(repository["_github_token"])
                environment = {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": "Authorization: Basic "
                    + base64.b64encode(("x-access-token:" + token).encode()).decode(),
                }
            git_call(
                base,
                *args,
                "origin",
                f"refs/heads/{selected}:refs/heads/{selected}",
                timeout=120,
                extra_env=environment,
            )
        access.audit(user, "git.push", repository.get("project_id"), id)
        return {"ok": True, "branch": selected}

    @app.put("/api/repositories/{id}/git/github-credential")
    def credential(
        id: str, body: GithubCredentialInput, request: Request, user=Depends(admin)
    ):
        store, repository, _ = lookup(request, id, user, "edit")
        github_repository(repository["url"])
        if body.token:
            if len(body.token) < 20:
                raise ValueError("Enter a repository-scoped GitHub token")
            repository["_github_token"] = SecretBox(store.directory).encrypt(body.token)
            store.put("repositories", repository)
        return {"has_github_token": bool(repository.get("_github_token"))}

    @app.post("/api/repositories/{id}/git/pull-request", status_code=201)
    def pull_request(
        id: str, body: PullRequestInput, request: Request, user=Depends(authenticated)
    ):
        store, repository, base = lookup(request, id, user, "edit")
        owner, name = github_repository(repository["url"])
        branch = git_text(base, "symbolic-ref", "--quiet", "--short", "HEAD")
        branch_name(base, body.base)
        if not branch or branch == body.base:
            raise ValueError(
                "Choose a feature branch different from the pull request base"
            )
        if not repository.get("_github_token"):
            raise HTTPException(
                409,
                "An administrator must configure a GitHub pull-request token for this repository",
            )
        token = SecretBox(store.directory).decrypt(repository["_github_token"])
        try:
            response = httpx.post(
                f"https://api.github.com/repos/{owner}/{name}/pulls",
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/vnd.github+json",
                },
                json={
                    "title": body.title,
                    "body": body.body,
                    "head": branch,
                    "base": body.base,
                    "draft": body.draft,
                },
                timeout=20,
                follow_redirects=False,
            )
            if response.status_code != 201:
                raise HTTPException(
                    502,
                    f"GitHub did not create the pull request (HTTP {response.status_code}). Check permissions and whether a pull request already exists",
                )
            value = response.json()
            number = int(value["number"])
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise HTTPException(
                502,
                "GitHub pull-request creation failed. Check the repository and token permissions",
            ) from exc
        access.audit(user, "git.pull_request", repository.get("project_id"), id)
        return {
            "number": number,
            "url": f"https://github.com/{owner}/{name}/pull/{number}",
        }
