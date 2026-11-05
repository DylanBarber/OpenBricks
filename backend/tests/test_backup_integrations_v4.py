"""Synthetic recovery attachments; no live object store or catalog is contacted."""

import hashlib
import io
import json
from types import SimpleNamespace

import boto3
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from fastapi import HTTPException
import pytest

from openbricks import backup_integrations as recovery
from openbricks.store import Store


def error(code):
    return ClientError({"Error": {"Code": code, "Message": "synthetic"}}, "PutObject")


class S3:
    def __init__(self):
        self.objects, self.calls, self.uploads = {}, [], {}
        self.closed = False
        self.race = False

    def head_object(self, **options):
        self.calls.append(("head", options))
        item = self.objects.get((options["Bucket"], options["Key"]))
        if item is None:
            if self.race:
                self.objects[(options["Bucket"], options["Key"])] = (
                    b"concurrent writer",
                    {},
                )
            raise error("NoSuchKey")
        body, metadata = item
        return {
            "ContentLength": len(body),
            "Metadata": metadata,
            "ETag": hashlib.md5(body).hexdigest(),
        }

    def get_object(self, **options):
        self.calls.append(("get", options))
        body, _ = self.objects[(options["Bucket"], options["Key"])]
        etag = hashlib.md5(body).hexdigest()
        assert options.get("IfMatch") == etag
        return {"Body": io.BytesIO(body), "ETag": etag, "ContentType": "text/plain"}

    def put_object(self, **options):
        self.calls.append(("put", options))
        assert options["IfNoneMatch"] == "*"
        key = options["Bucket"], options["Key"]
        if key in self.objects:
            raise error("PreconditionFailed")
        self.objects[key] = options["Body"].read(), options["Metadata"]
        return {"ETag": "new"}

    def list_objects_v2(self, **options):
        return {
            "Contents": [
                {"Key": key, "Size": len(body), "ETag": hashlib.md5(body).hexdigest()}
                for (bucket, key), (body, _) in self.objects.items()
                if bucket == options["Bucket"] and key.startswith(options["Prefix"])
            ]
        }

    def create_multipart_upload(self, **options):
        self.calls.append(("create", options))
        self.uploads["upload"] = []
        return {"UploadId": "upload"}

    def upload_part(self, **options):
        self.calls.append(("part", options))
        self.uploads[options["UploadId"]].append(options["Body"])
        return {"ETag": str(options["PartNumber"])}

    def complete_multipart_upload(self, **options):
        self.calls.append(("complete", options))
        assert options["IfNoneMatch"] == "*"
        if self.race:
            raise error("ConditionalRequestConflict")
        return {"ETag": "done"}

    def abort_multipart_upload(self, **options):
        self.calls.append(("abort", options))

    def close(self):
        self.closed = True


def manifest(store, bodies=(b"one", b"two"), catalog=None):
    root = store.directory / "recovery"
    root.mkdir(exist_ok=True)
    objects = []
    for index, body in enumerate(bodies):
        name = f"object-{index}.bin"
        (root / name).write_bytes(body)
        objects.append(
            {
                "connection_id": "source",
                "bucket": "test-bucket",
                "key": f"folder/{index}",
                "file": name,
                "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "content_type": "text/plain",
            }
        )
    value = {"version": 1, "objects": objects, "catalog": catalog}
    (root / "integrations.json").write_text(json.dumps(value))
    return value


def restore(store, **options):
    plan = recovery.recovery_plan(
        store, {"source": "target"}, options.get("include_catalog", False)
    )
    return recovery.restore_integrations(
        store, "default", {"source": "target"}, plan["plan_hash"], **options
    )


def test_export_restore_roundtrip_checks_inventory_and_idempotent_content(
    tmp_path, monkeypatch
):
    source, target = Store(tmp_path / "source"), Store(tmp_path / "target")
    client = S3()
    client.objects[("test-bucket", "folder/MiXeD.csv")] = (b"id,value\n1,ok\n", {})
    monkeypatch.setattr(recovery, "s3_client", lambda *args: client)
    result = recovery.export_integrations(
        source,
        {
            "project_id": "default",
            "storage_sources": [
                {
                    "connection_id": "source",
                    "bucket": "test-bucket",
                    "prefix": "folder/",
                }
            ],
        },
        target.directory,
    )
    assert result == {"objects": 1, "object_bytes": 14, "catalog_tables": 0}
    assert client.closed
    client.objects.clear()
    assert restore(target)["objects_restored"] == 1
    second = restore(target)
    assert second["objects_restored"] == 0 and second["existing_identical_skipped"] == 1
    assert any(name == "get" and "IfMatch" in call for name, call in client.calls)


def test_concurrent_create_after_head_is_never_overwritten(tmp_path, monkeypatch):
    store, client = Store(tmp_path / "workspace"), S3()
    manifest(store, (b"backup bytes",))
    client.race = True
    monkeypatch.setattr(recovery, "s3_client", lambda *args: client)
    with pytest.raises(HTTPException) as caught:
        restore(store)
    assert caught.value.status_code == 409
    assert client.objects[("test-bucket", "folder/0")][0] == b"concurrent writer"
    assert client.closed


def test_metadata_hash_does_not_falsely_skip_different_bytes(tmp_path, monkeypatch):
    store, client = Store(tmp_path / "workspace"), S3()
    item = manifest(store, (b"good",))["objects"][0]
    client.objects[("test-bucket", "folder/0")] = (
        b"evil",
        {"openbricks-sha256": item["sha256"]},
    )
    monkeypatch.setattr(recovery, "s3_client", lambda *args: client)
    with pytest.raises(HTTPException) as caught:
        restore(store)
    assert caught.value.status_code == 409
    assert not any(name == "put" for name, _ in client.calls)


def test_all_attachments_are_checked_before_first_external_write(tmp_path, monkeypatch):
    store = Store(tmp_path / "workspace")
    manifest(store)
    (store.directory / "recovery/object-1.bin").write_bytes(b"corrupt")
    monkeypatch.setattr(
        recovery,
        "s3_client",
        lambda *args: pytest.fail("external connection before full validation"),
    )
    with pytest.raises(ValueError, match="checksum"):
        restore(store)


def test_multipart_commit_is_conditional_and_aborts_on_conflict_or_cancel(
    tmp_path, monkeypatch
):
    path = tmp_path / "large.bin"
    path.write_bytes(b"0123456789abcdef")
    monkeypatch.setattr(recovery, "SINGLE_UPLOAD_LIMIT", 1)
    monkeypatch.setattr(recovery, "MULTIPART_CHUNK_SIZE", 5)
    client = S3()
    recovery.upload_absent(
        client, path, "test-bucket", "key", sha256="sha", metadata={"run": "1"}
    )
    complete = next(call for name, call in client.calls if name == "complete")
    assert len(complete["MultipartUpload"]["Parts"]) == 4
    assert not any(name == "abort" for name, _ in client.calls)
    assert client.calls[0][1]["Metadata"] == {"run": "1", "openbricks-sha256": "sha"}
    client.race = True
    with pytest.raises(HTTPException):
        recovery.upload_absent(client, path, "test-bucket", "key", sha256="sha")
    assert client.calls[-1][0] == "abort"
    client.calls.clear()

    def cancelled():
        if any(name == "part" for name, _ in client.calls):
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        recovery.upload_absent(
            client, path, "test-bucket", "key", sha256="sha", check=cancelled
        )
    assert client.calls[-1][0] == "abort"


def test_small_upload_matches_real_botocore_conditional_wire_contract(tmp_path):
    path = tmp_path / "small.bin"
    path.write_bytes(b"synthetic")
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="synthetic",
        aws_secret_access_key="synthetic",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "put_object",
            {"ETag": '"etag"'},
            {
                "Bucket": "test-bucket",
                "Key": "key",
                "Metadata": {"openbricks-sha256": "digest"},
                "ContentType": "text/plain",
                "ContentLength": 9,
                "IfNoneMatch": "*",
                "Body": ANY,
            },
        )
        recovery.upload_absent(
            client,
            path,
            "test-bucket",
            "key",
            sha256="digest",
            content_type="text/plain",
        )
        stubber.assert_no_pending_responses()
    client.close()


def test_real_sdk_serializes_conditional_header_and_stream_without_network(tmp_path):
    path = tmp_path / "wire.bin"
    path.write_bytes(b"synthetic wire content")
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        endpoint_url="https://never-contact.invalid",
        aws_access_key_id="synthetic",
        aws_secret_access_key="synthetic",
    )
    sent = []

    class EmptyResponse:
        def stream(self, *args, **kwargs):
            yield b""

    def transport(request, **kwargs):
        assert request.headers["If-None-Match"] == b"*"
        body = request.body.read()
        assert b"synthetic wire content" in body
        sent.append(True)
        return AWSResponse(request.url, 200, {"ETag": '"synthetic"'}, EmptyResponse())

    client.meta.events.register("before-send.s3.PutObject", transport)
    recovery.upload_absent(client, path, "test-bucket", "key", sha256="digest")
    assert sent == [True]
    client.close()


def catalog_fixture():
    return {
        "catalogs": [{"name": "analytics", "properties": {"purpose": "test"}}],
        "schemas": [
            {"name": "sales", "catalog_name": "analytics", "comment": "sales schema"}
        ],
        "tables": [
            {
                "name": "orders",
                "catalog_name": "analytics",
                "schema_name": "sales",
                "table_type": "EXTERNAL",
                "data_source_format": "ORC",
                "storage_location": "s3://test-bucket/orders",
                "properties": {"version": "1"},
                "columns": [
                    {
                        "name": "amount",
                        "type_name": "DECIMAL",
                        "type_text": "decimal(12,2)",
                        "type_json": json.dumps(
                            {
                                "name": "amount",
                                "type": "decimal(12,2)",
                                "nullable": False,
                                "metadata": {"comment": "Amount"},
                            }
                        ),
                        "type_precision": 12,
                        "type_scale": 2,
                        "position": 0,
                        "nullable": False,
                        "comment": "Amount",
                        "partition_index": 0,
                    }
                ],
            }
        ],
    }


def test_catalog_restore_preserves_properties_column_comments_types_and_partitioning(
    tmp_path, monkeypatch
):
    store, catalog = Store(tmp_path / "workspace"), catalog_fixture()
    manifest(store, (), catalog)
    calls = []
    monkeypatch.setattr(
        "openbricks.catalog.catalog_request",
        lambda method, path, **kw: calls.append((method, path, kw["body"])) or {},
    )
    result = restore(store, include_catalog=True)
    assert result["catalog_objects_created"] == 3
    assert [kind for _, kind, _ in calls] == ["catalogs", "schemas", "tables"]
    table = calls[-1][2]
    assert table["properties"] == catalog["tables"][0]["properties"]
    assert table["columns"] == catalog["tables"][0]["columns"]
    assert table["data_source_format"] == "ORC"


def test_unsupported_managed_catalog_fails_before_storage_mutation(
    tmp_path, monkeypatch
):
    store, catalog = Store(tmp_path / "workspace"), catalog_fixture()
    catalog["tables"][0]["table_type"] = "MANAGED"
    manifest(store, (b"one",), catalog)
    monkeypatch.setattr(
        recovery,
        "s3_client",
        lambda *args: pytest.fail("mutated before catalog preflight"),
    )
    with pytest.raises(ValueError, match="external catalog tables only"):
        restore(store, include_catalog=True)


def test_existing_catalog_skips_are_reported_separately(tmp_path, monkeypatch):
    store = Store(tmp_path / "workspace")
    manifest(store, (), catalog_fixture())

    def exists(*args, **kw):
        raise HTTPException(409, "exists")

    monkeypatch.setattr("openbricks.catalog.catalog_request", exists)
    result = restore(store, include_catalog=True)
    assert (
        result["catalog_existing_skipped"] == 3
        and result["existing_identical_skipped"] == 0
    )


def test_catalog_table_pagination_obeys_api_cap_and_rejects_stuck_token(monkeypatch):
    calls = []

    def page(method, path, **kw):
        calls.append(kw["params"])
        return {"tables": [], "next_page_token": "same"}

    monkeypatch.setattr("openbricks.catalog.catalog_request", page)
    with pytest.raises(ValueError, match="pagination"):
        recovery.catalog_collection("tables", "tables")
    assert len(calls) == 2 and all(item["max_results"] == 50 for item in calls)


def test_catalog_export_budget_is_enforced_while_collecting_pages(monkeypatch):
    calls = []

    def page(method, path, **kw):
        calls.append(path)
        return {
            "catalogs": [{"name": "catalog", "comment": "x" * 300}],
            "next_page_token": "more",
        }

    monkeypatch.setattr(recovery, "MANIFEST_LIMIT", 128)
    monkeypatch.setattr("openbricks.catalog.catalog_request", page)
    with pytest.raises(ValueError, match="budget"):
        recovery.export_catalog()
    assert calls == ["catalogs"]


def test_catalog_export_checks_cancellation_between_pages(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "openbricks.catalog.catalog_request",
        lambda *args, **kw: calls.append(True)
        or {"tables": [], "next_page_token": "more"},
    )

    def check():
        if calls:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        recovery.catalog_collection("tables", "tables", check=check)
    assert calls == [True]


def test_plan_hash_detects_changed_mapping_or_manifest(tmp_path):
    store = Store(tmp_path / "workspace")
    manifest(store, (b"one",))
    before = recovery.recovery_plan(store, {"source": "first"})
    with pytest.raises(HTTPException) as caught:
        recovery.restore_integrations(
            store, "default", {"source": "second"}, before["plan_hash"]
        )
    assert caught.value.status_code == 409


def test_export_rejects_overlapping_source_aliases_after_connection_edits(
    tmp_path, monkeypatch
):
    client = S3()
    client.meta = SimpleNamespace(endpoint_url="https://OBJECTS.example:443/")
    client.objects[("test-bucket", "folder/file")] = (b"one", {})
    monkeypatch.setattr(recovery, "s3_client", lambda *args: client)
    source = Store(tmp_path / "source")
    with pytest.raises(ValueError, match="sources overlap"):
        recovery.export_integrations(
            source,
            {
                "project_id": "default",
                "storage_sources": [
                    {
                        "connection_id": "first",
                        "bucket": "test-bucket",
                        "prefix": "folder/",
                    },
                    {
                        "connection_id": "alias",
                        "bucket": "test-bucket",
                        "prefix": "folder/file",
                    },
                ],
            },
            tmp_path / "export",
        )
    assert client.closed


def test_conflicting_destination_aliases_fail_before_first_write(tmp_path, monkeypatch):
    store = Store(tmp_path / "workspace")
    value = manifest(store)
    value["objects"][1].update(connection_id="second-source", key="folder/0")
    (store.directory / "recovery/integrations.json").write_text(json.dumps(value))
    clients = [S3(), S3()]
    for client in clients:
        client.meta = SimpleNamespace(endpoint_url="https://objects.example")
    monkeypatch.setattr(
        recovery,
        "s3_client",
        lambda store, project, identifier: clients[
            0 if identifier == "target-1" else 1
        ],
    )
    mappings = {"source": "target-1", "second-source": "target-2"}
    plan = recovery.recovery_plan(store, mappings)
    with pytest.raises(ValueError, match="same destination"):
        recovery.restore_integrations(store, "default", mappings, plan["plan_hash"])
    assert all(client.closed and not client.calls for client in clients)
