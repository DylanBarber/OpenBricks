"""Portable S3 object and Unity Catalog metadata recovery attachments."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import HTTPException

from .connectors import connections_for_runtime
from .security import confined_path

SINGLE_UPLOAD_LIMIT = 64 * 1024**2
MULTIPART_CHUNK_SIZE = 8 * 1024**2
MANIFEST_LIMIT = 20 * 1024**2


def _endpoint_identity(client, connection_id):
    endpoint = getattr(getattr(client, "meta", None), "endpoint_url", None)
    if not endpoint:
        return ("connection", connection_id)
    parsed = urlsplit(endpoint)
    host = (parsed.hostname or "").lower()
    if host.endswith(".amazonaws.com") or host.endswith(".amazonaws.com.cn"):
        return ("aws-s3", "china" if host.endswith(".cn") else "global")
    return (
        parsed.scheme.lower(),
        host,
        parsed.port or (443 if parsed.scheme == "https" else 80),
        parsed.path.rstrip("/"),
    )


class _CheckedReader:
    def __init__(self, stream, check):
        self.stream, self.check = stream, check

    def read(self, size=-1):
        self.check()
        return self.stream.read(size)

    def seek(self, *args):
        return self.stream.seek(*args)

    def tell(self):
        return self.stream.tell()


def upload_absent(
    client,
    path,
    bucket,
    key,
    *,
    sha256,
    content_type="application/octet-stream",
    metadata=None,
    check=lambda: None,
):
    """Atomically create an object, including the final multipart commit.

    A preceding HEAD is only an optimization: the write itself must be
    conditional. Providers lacking conditional writes fail closed.
    """
    path = Path(path)
    options = {
        "Bucket": bucket,
        "Key": key,
        "Metadata": {**(metadata or {}), "openbricks-sha256": sha256},
        "ContentType": content_type,
    }
    upload_id = None
    try:
        check()
        if path.stat().st_size <= SINGLE_UPLOAD_LIMIT:
            with path.open("rb") as stream:
                client.put_object(
                    **options,
                    IfNoneMatch="*",
                    ContentLength=path.stat().st_size,
                    Body=_CheckedReader(stream, check),
                )
            return
        upload_id = client.create_multipart_upload(**options)["UploadId"]
        parts = []
        with path.open("rb") as stream:
            while chunk := stream.read(MULTIPART_CHUNK_SIZE):
                check()
                number = len(parts) + 1
                response = client.upload_part(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=number,
                    Body=chunk,
                )
                parts.append({"PartNumber": number, "ETag": response["ETag"]})
        check()
        client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
            IfNoneMatch="*",
        )
        upload_id = None
    except Exception as exc:
        if str(getattr(exc, "response", {}).get("Error", {}).get("Code")) in (
            "412",
            "PreconditionFailed",
            "409",
            "ConditionalRequestConflict",
        ):
            raise HTTPException(
                409,
                "A destination object already exists or changed concurrently; recovery never overwrites it",
            ) from exc
        raise
    finally:
        if upload_id is not None:
            try:
                client.abort_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id
                )
            except Exception:
                # Preserve the write/cancellation error. Bucket lifecycle rules
                # can remove an orphan if the provider is unavailable to abort.
                pass


def s3_client(store, project_id, connection_id):
    import boto3
    from botocore.config import Config

    values = connections_for_runtime(store, project_id, [connection_id])
    record = next(
        (
            item
            for item in values
            if item["id"] == connection_id and item["type"] == "s3"
        ),
        None,
    )
    if not record:
        raise ValueError(
            "Backup storage connection is not assigned to the selected project"
        )
    config, secrets = record["config"], record["secrets"]
    return boto3.client(
        "s3",
        endpoint_url=config.get("endpoint_url") or None,
        region_name=config.get("region", "us-east-1"),
        aws_access_key_id=secrets["access_key"],
        aws_secret_access_key=secrets["secret_key"],
        aws_session_token=secrets.get("session_token") or None,
        verify=config.get("verify_tls", True),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": config.get("addressing_style", "path")},
            connect_timeout=4,
            read_timeout=10,
            retries={"max_attempts": 1},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def inventory(client, bucket, prefix, check=lambda: None):
    values, continuation = [], None
    while True:
        check()
        args = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if continuation:
            args["ContinuationToken"] = continuation
        page = client.list_objects_v2(**args)
        values.extend(
            {
                "key": item["Key"],
                "size": int(item["Size"]),
                "etag": item.get("ETag", ""),
            }
            for item in page.get("Contents", [])
        )
        if len(values) > 10000 or sum(item["size"] for item in values) > 20 * 1024**3:
            raise ValueError(
                "Selected storage exceeds the 10,000-object / 20 GiB recovery attachment limit"
            )
        continuation = page.get("NextContinuationToken")
        if not continuation:
            break
    return sorted(values, key=lambda item: item["key"])


def catalog_collection(path, key, params=None, check=lambda: None):
    from .catalog import catalog_request

    values, page_token, seen, budget = [], None, set(), 0
    while True:
        check()
        # Unity Catalog's table endpoint caps max_results at 50.
        options = {**(params or {}), "max_results": 50}
        if page_token:
            options["page_token"] = page_token
        page = catalog_request("GET", path, params=options)
        items = page.get(key, [])
        if not isinstance(items, list):
            raise ValueError("Invalid catalog metadata collection")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Invalid catalog metadata object")
            budget += len(json.dumps(item, ensure_ascii=False).encode())
            if budget > MANIFEST_LIMIT:
                raise ValueError(
                    "Catalog metadata exceeds the recovery attachment budget"
                )
            values.append(item)
        if len(values) > 20000:
            raise ValueError(
                "Catalog metadata exceeds this recovery format's object limit"
            )
        page_token = page.get("next_page_token")
        if not page_token:
            return values
        if page_token in seen or len(seen) >= 1000:
            raise ValueError("Catalog metadata pagination did not finish")
        seen.add(page_token)


def export_catalog(check=lambda: None):
    from .catalog import catalog_request

    result = {"catalogs": [], "schemas": [], "tables": []}
    budget = 0

    def append(kind, value):
        nonlocal budget
        budget += len(json.dumps(value, ensure_ascii=False).encode())
        if budget > MANIFEST_LIMIT or len(result[kind]) >= 20000:
            raise ValueError("Catalog metadata exceeds the recovery attachment budget")
        result[kind].append(value)

    for catalog in catalog_collection("catalogs", "catalogs", check=check):
        check()
        append(
            "catalogs",
            {
                key: catalog[key]
                for key in ("name", "comment", "properties")
                if key in catalog
            },
        )
        for schema in catalog_collection(
            "schemas", "schemas", {"catalog_name": catalog["name"]}, check=check
        ):
            check()
            append(
                "schemas",
                {
                    key: schema[key]
                    for key in ("name", "catalog_name", "comment", "properties")
                    if key in schema
                },
            )
            for table in catalog_collection(
                "tables",
                "tables",
                {"catalog_name": catalog["name"], "schema_name": schema["name"]},
                check=check,
            ):
                check()
                full = table.get("full_name") or ".".join(
                    (catalog["name"], schema["name"], table["name"])
                )
                detail = catalog_request("GET", "tables/" + quote(full, safe=""))
                append(
                    "tables",
                    {
                        key: detail[key]
                        for key in (
                            "name",
                            "catalog_name",
                            "schema_name",
                            "data_source_format",
                            "storage_location",
                            "columns",
                            "comment",
                            "properties",
                            "table_type",
                        )
                        if key in detail
                    },
                )
    return result


def export_integrations(store, policy, destination, check=lambda: None):
    root = Path(destination) / "recovery"
    root.mkdir(parents=True)
    manifest = {
        "version": 1,
        "objects": [],
        "catalog": None,
        "consistency": "OpenBricks execution paused; selected object inventories verified unchanged. Stop external writers for a coordinated snapshot.",
    }
    total, seen = 0, set()
    for index, source in enumerate(policy.get("storage_sources", [])):
        client = s3_client(store, policy["project_id"], source["connection_id"])
        try:
            endpoint = _endpoint_identity(client, source["connection_id"])
            before = inventory(
                client, source["bucket"], source.get("prefix", ""), check
            )
            for item in before:
                check()
                object_id = (endpoint, source["bucket"], item["key"])
                if object_id in seen:
                    raise ValueError(
                        "Selected recovery storage sources overlap; choose nonoverlapping prefixes"
                    )
                seen.add(object_id)
                if not item["etag"]:
                    raise ValueError(
                        "Storage provider must return object ETags for a consistent recovery snapshot"
                    )
                relative = (
                    f"objects/{index}/"
                    + hashlib.sha256(item["key"].encode()).hexdigest()
                    + ".bin"
                )
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                args = {"Bucket": source["bucket"], "Key": item["key"]}
                if item["etag"]:
                    args["IfMatch"] = item["etag"]
                response = client.get_object(**args)
                stream, digest, size = response["Body"], hashlib.sha256(), 0
                try:
                    with target.open("wb") as output:
                        while chunk := stream.read(1024 * 1024):
                            check()
                            size += len(chunk)
                            total += len(chunk)
                            if total > 20 * 1024**3 or size > item["size"]:
                                raise ValueError(
                                    "Storage changed or exceeded the recovery snapshot limit"
                                )
                            digest.update(chunk)
                            output.write(chunk)
                finally:
                    stream.close()
                if size != item["size"] or (
                    response.get("ETag") and response["ETag"] != item["etag"]
                ):
                    raise ValueError("Storage changed during the recovery snapshot")
                manifest["objects"].append(
                    {
                        **item,
                        "connection_id": source["connection_id"],
                        "bucket": source["bucket"],
                        "file": relative,
                        "sha256": digest.hexdigest(),
                        "content_type": response.get(
                            "ContentType", "application/octet-stream"
                        ),
                    }
                )
                if len(manifest["objects"]) > 10000:
                    raise ValueError("Recovery snapshot exceeds 10,000 objects")
            if (
                inventory(client, source["bucket"], source.get("prefix", ""), check)
                != before
            ):
                raise ValueError(
                    "Storage changed during the recovery snapshot; retry after writers are idle"
                )
        finally:
            client.close()
    if policy.get("include_catalog"):
        manifest["catalog"] = export_catalog(check)
    content = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    if len(content) > MANIFEST_LIMIT:
        raise ValueError("Recovery attachment manifest exceeds 20 MiB")
    (root / "integrations.json").write_bytes(content)
    return {
        "objects": len(manifest["objects"]),
        "object_bytes": total,
        "catalog_tables": len((manifest["catalog"] or {}).get("tables", [])),
    }


def load_recovery(store):
    path = store.directory / "recovery" / "integrations.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MANIFEST_LIMIT:
        raise ValueError("This workspace has no restored integration attachments")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(value.get("objects"), list)
        or len(value["objects"]) > 10000
    ):
        raise ValueError("Invalid integration recovery manifest")
    total, identities = 0, set()
    for item in value["objects"]:
        if not isinstance(item, dict) or any(
            not isinstance(item.get(key), str)
            or not item[key]
            or len(item[key]) > limit
            for key, limit in (
                ("key", 1024),
                ("bucket", 255),
                ("connection_id", 255),
                ("file", 1024),
                ("sha256", 64),
            )
        ):
            raise ValueError("Invalid integration recovery object")
        size = item.get("size")
        if (
            type(size) is not int
            or size < 0
            or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
        ):
            raise ValueError("Invalid integration recovery object checksum or size")
        content_type = item.get("content_type", "application/octet-stream")
        if (
            not isinstance(content_type, str)
            or len(content_type) > 255
            or any(c in content_type for c in "\r\n\0")
        ):
            raise ValueError("Invalid integration recovery content type")
        identity = (item["connection_id"], item["bucket"], item["key"])
        if identity in identities:
            raise ValueError("Duplicate integration recovery object")
        identities.add(identity)
        total += size
    if total > 20 * 1024**3:
        raise ValueError("Integration recovery attachments exceed 20 GiB")
    return value


def catalog_payloads(data):
    """Validate and retain the writable Unity Catalog 0.7 metadata fields.

    Owners, grants, IDs and managed-table storage allocation are deliberately
    outside this external-table recovery format.
    """
    from .catalog import identifier, storage_location, TYPES

    def name(value):
        if not isinstance(value, str):
            raise ValueError("Invalid recovery catalog name")
        return identifier(value)

    if not isinstance(data, dict):
        raise ValueError("Invalid recovery catalog metadata")
    result = []
    for kind in ("catalogs", "schemas", "tables"):
        items = data.get(kind, [])
        if not isinstance(items, list) or len(items) > 20000:
            raise ValueError("Invalid recovery catalog collection")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Invalid recovery catalog object")
            payload = {"name": name(item.get("name", ""))}
            if kind != "catalogs":
                payload["catalog_name"] = name(item.get("catalog_name", ""))
            for key in ("comment", "properties"):
                if item.get(key) is not None:
                    payload[key] = item[key]
            if "comment" in payload and (
                not isinstance(payload["comment"], str)
                or len(payload["comment"]) > 4000
            ):
                raise ValueError("Invalid recovery catalog comment")
            properties = payload.get("properties", {})
            if (
                not isinstance(properties, dict)
                or len(properties) > 1000
                or any(
                    not isinstance(k, str)
                    or not isinstance(v, str)
                    or len(k) > 4000
                    or len(v) > 20000
                    for k, v in properties.items()
                )
            ):
                raise ValueError("Invalid recovery catalog properties")
            if kind == "tables":
                if item.get("table_type", "EXTERNAL") != "EXTERNAL":
                    raise ValueError(
                        "Recovery supports external catalog tables only; managed tables and views require their native recovery process"
                    )
                location = item.get("storage_location", "")
                if not isinstance(location, str) or len(location) > 4000:
                    raise ValueError("Invalid recovery catalog storage location")
                payload.update(
                    schema_name=name(item.get("schema_name", "")),
                    table_type="EXTERNAL",
                    storage_location=storage_location(location),
                )
                format_name = item.get("data_source_format")
                if format_name not in (
                    "DELTA",
                    "ICEBERG",
                    "CSV",
                    "JSON",
                    "AVRO",
                    "PARQUET",
                    "ORC",
                    "TEXT",
                ):
                    raise ValueError("Invalid recovery catalog table format")
                payload["data_source_format"] = format_name
                columns = item.get("columns")
                if not isinstance(columns, list) or not 1 <= len(columns) <= 2000:
                    raise ValueError("Invalid recovery catalog columns")
                payload["columns"], names = [], set()
                for index, column in enumerate(columns):
                    if not isinstance(column, dict):
                        raise ValueError("Invalid recovery catalog column")
                    column_name = name(column.get("name", ""))
                    if (
                        column_name.casefold() in names
                        or column.get("position", index) != index
                    ):
                        raise ValueError(
                            "Recovery column names or positions are invalid"
                        )
                    names.add(column_name.casefold())
                    if column.get("type_name") not in TYPES | {
                        "USER_DEFINED_TYPE",
                        "TABLE_TYPE",
                    }:
                        raise ValueError("Invalid recovery catalog column type")
                    copied = {
                        key: column[key]
                        for key in (
                            "name",
                            "type_name",
                            "type_text",
                            "type_json",
                            "type_precision",
                            "type_scale",
                            "type_interval_type",
                            "position",
                            "comment",
                            "nullable",
                            "partition_index",
                        )
                        if column.get(key) is not None
                    }
                    for key, limit in (
                        ("type_text", 4000),
                        ("type_json", 20000),
                        ("comment", 4000),
                        ("type_interval_type", 4000),
                    ):
                        if key in copied and (
                            not isinstance(copied[key], str) or len(copied[key]) > limit
                        ):
                            raise ValueError("Invalid recovery catalog column metadata")
                    if "type_json" in copied:
                        json.loads(copied["type_json"])
                    for key in (
                        "type_precision",
                        "type_scale",
                        "position",
                        "partition_index",
                    ):
                        if key in copied and (
                            type(copied[key]) is not int
                            or not -1 <= copied[key] <= 2000
                        ):
                            raise ValueError(
                                "Invalid recovery catalog numeric column metadata"
                            )
                    if "nullable" in copied and type(copied["nullable"]) is not bool:
                        raise ValueError("Invalid recovery catalog column nullability")
                    copied.setdefault("position", index)
                    payload["columns"].append(copied)
            result.append((kind, payload))
    return result


def verified_attachments(store, manifest, check):
    """Check every local attachment before starting any external mutation."""
    paths = []
    for item in manifest["objects"]:
        check()
        path = confined_path(store.directory / "recovery", item["file"])
        if not path.is_file() or path.is_symlink():
            raise ValueError("Recovery object file is missing")
        digest, size = hashlib.sha256(), 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                check()
                digest.update(chunk)
                size += len(chunk)
        if digest.hexdigest() != item["sha256"] or size != item["size"]:
            raise ValueError("Recovery object checksum mismatch")
        paths.append(path)
    return paths


def identical_object(client, item, existing, check):
    if (
        existing.get("Metadata", {}).get("openbricks-sha256") != item["sha256"]
        or existing.get("ContentLength") != item["size"]
    ):
        return False
    etag = existing.get("ETag")
    if not etag:
        return False
    response = client.get_object(Bucket=item["bucket"], Key=item["key"], IfMatch=etag)
    stream, digest, size = response["Body"], hashlib.sha256(), 0
    try:
        while chunk := stream.read(1024 * 1024):
            check()
            size += len(chunk)
            if size > item["size"]:
                return False
            digest.update(chunk)
    finally:
        stream.close()
    return size == item["size"] and digest.hexdigest() == item["sha256"]


def recovery_plan(store, mappings, include_catalog=False):
    manifest = load_recovery(store)
    if not isinstance(mappings, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or not v
        for k, v in mappings.items()
    ):
        raise ValueError("Invalid recovery destination mapping")
    if include_catalog:
        catalog_payloads(manifest.get("catalog") or {})
    plan = {
        "objects": len(manifest["objects"]),
        "bytes": sum(item["size"] for item in manifest["objects"]),
        "catalog_tables": (
            len((manifest.get("catalog") or {}).get("tables", []))
            if include_catalog
            else 0
        ),
        "mappings": mappings,
        "include_catalog": include_catalog,
        "overwrites": False,
    }
    missing = {item["connection_id"] for item in manifest["objects"]} - set(mappings)
    if missing:
        raise ValueError(
            "Map each backed-up storage connection to a current destination"
        )
    identity = {
        "manifest": hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest(),
        **plan,
    }
    plan["plan_hash"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()
    return plan


def restore_integrations(
    store,
    project_id,
    mappings,
    expected_plan_hash,
    include_catalog=False,
    check=lambda: None,
):
    plan = recovery_plan(store, mappings, include_catalog)
    if plan["plan_hash"] != expected_plan_hash:
        raise HTTPException(409, "Recovery plan changed; preview it again")
    manifest = load_recovery(store)
    paths = verified_attachments(store, manifest, check)
    catalog = catalog_payloads(manifest.get("catalog") or {}) if include_catalog else []
    clients, restored, skipped = {}, 0, 0
    try:
        destinations = {}
        for item in manifest["objects"]:
            check()
            connection_id = mappings[item["connection_id"]]
            if connection_id not in clients:
                clients[connection_id] = s3_client(store, project_id, connection_id)
            target = (
                _endpoint_identity(clients[connection_id], connection_id),
                item["bucket"],
                item["key"],
            )
            if target in destinations and destinations[target] != item["sha256"]:
                raise ValueError(
                    "Recovery mappings assign different source content to the same destination object"
                )
            destinations[target] = item["sha256"]
        for item, path in zip(manifest["objects"], paths):
            check()
            connection_id = mappings[item["connection_id"]]
            client = clients[connection_id]
            try:
                existing = client.head_object(Bucket=item["bucket"], Key=item["key"])
            except Exception as exc:
                if str(
                    getattr(exc, "response", {}).get("Error", {}).get("Code")
                ) not in ("404", "NoSuchKey", "NotFound"):
                    raise
                existing = None
            if existing:
                if identical_object(client, item, existing, check):
                    skipped += 1
                    continue
                raise HTTPException(
                    409,
                    "A destination object already exists; recovery never overwrites it. Use an empty destination bucket",
                )
            upload_absent(
                client,
                path,
                item["bucket"],
                item["key"],
                sha256=item["sha256"],
                content_type=item.get("content_type", "application/octet-stream"),
                check=check,
            )
            restored += 1
        catalog_restored, catalog_skipped = 0, 0
        if include_catalog:
            from .catalog import catalog_request

            for kind, payload in catalog:
                check()
                try:
                    catalog_request("POST", kind, body=payload)
                    catalog_restored += 1
                except HTTPException as exc:
                    if exc.status_code != 409:
                        raise
                    # Never update or replace existing catalog metadata. Existing
                    # catalog objects are not claimed to be byte-identical.
                    catalog_skipped += 1
        return {
            "objects_restored": restored,
            "existing_identical_skipped": skipped,
            "catalog_objects_created": catalog_restored,
            "catalog_existing_skipped": catalog_skipped,
            "overwrites": False,
        }
    finally:
        for client in clients.values():
            client.close()
