import json

import httpx
import pytest
from fastapi.testclient import TestClient

from openbricks import catalog
from openbricks.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "test-password-long-123")
    monkeypatch.delenv("OPENBRICKS_COMPUTE_URL", raising=False)
    monkeypatch.delenv("OPENBRICKS_CATALOG_URL", raising=False)
    monkeypatch.delenv("OPENBRICKS_CATALOG_TOKEN", raising=False)
    with TestClient(create_app(tmp_path / "data")) as value:
        value.post(
            "/api/auth/login",
            json={"username": "admin", "password": "test-password-long-123"},
        ).raise_for_status()
        yield value


def mock_catalog(monkeypatch, handler):
    monkeypatch.setenv("OPENBRICKS_CATALOG_URL", "http://catalog:8080")
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        catalog,
        "catalog_client",
        lambda base: httpx.Client(base_url=base + "/", transport=transport),
    )


def test_catalog_optional_status_and_authentication(client, monkeypatch):
    assert client.get("/api/catalog/status").json() == {
        "configured": False,
        "available": False,
    }
    assert client.get("/api/catalog/catalogs").status_code == 503
    monkeypatch.setenv("OPENBRICKS_CATALOG_URL", "http://token:secret@catalog:8080")
    status = client.get("/api/catalog/status").json()
    assert (
        status["configured"]
        and not status["available"]
        and "secret" not in json.dumps(status)
    )
    client.post("/api/auth/logout").raise_for_status()
    assert client.get("/api/catalog/status").status_code == 401


def test_catalog_native_api_collections_and_external_table_metadata(
    client, monkeypatch
):
    calls = []

    def server(request):
        calls.append(request)
        assert request.url.path.startswith("/api/2.1/unity-catalog/")
        if request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(200, json={**body, "created_at": 1})
        if request.url.path.endswith("/catalogs"):
            return httpx.Response(200, json={"catalogs": [{"name": "retail"}]})
        if request.url.path.endswith("/schemas"):
            return httpx.Response(
                200, json={"schemas": [{"name": "sales", "catalog_name": "retail"}]}
            )
        if request.url.path.endswith("/tables"):
            return httpx.Response(
                200,
                json={
                    "tables": [
                        {
                            "name": "orders",
                            "catalog_name": "retail",
                            "schema_name": "sales",
                        }
                    ],
                    "next_page_token": "opaque-next",
                },
            )
        return httpx.Response(
            200,
            json={"name": "orders", "columns": [{"name": "id", "type_name": "INT"}]},
        )

    mock_catalog(monkeypatch, server)
    assert client.get("/api/catalog/status").json()["available"]
    assert client.get("/api/catalog/catalogs").json()["items"][0]["name"] == "retail"
    created = client.post(
        "/api/catalog/catalogs", json={"name": "retail", "comment": "Shared tables"}
    )
    assert created.status_code == 201
    schema = client.post(
        "/api/catalog/schemas", json={"name": "sales", "catalog_name": "retail"}
    )
    assert schema.status_code == 201
    assert (
        client.get("/api/catalog/schemas", params={"catalog_name": "retail"}).json()[
            "items"
        ][0]["name"]
        == "sales"
    )
    body = {
        "name": "orders",
        "catalog_name": "retail",
        "schema_name": "sales",
        "data_source_format": "PARQUET",
        "storage_location": "s3://warehouse/orders",
        "properties": {"source": "recovery-fixture"},
        "columns": [
            {
                "name": "id",
                "type_name": "INT",
                "type_text": "int",
                "nullable": False,
                "comment": "Stable primary key",
                "partition_index": 0,
            }
        ],
    }
    table = client.post("/api/catalog/tables", json=body)
    assert table.status_code == 201, table.text
    assert table.json()["table_type"] == "EXTERNAL"
    type_json = json.loads(table.json()["columns"][0]["type_json"])
    assert type_json == {
        "name": "id",
        "type": "integer",
        "nullable": False,
        "metadata": {},
    }
    assert table.json()["columns"][0]["position"] == 0
    assert table.json()["columns"][0]["comment"] == "Stable primary key"
    assert table.json()["columns"][0]["partition_index"] == 0
    assert table.json()["properties"] == {"source": "recovery-fixture"}
    page = client.get(
        "/api/catalog/tables",
        params={
            "catalog_name": "retail",
            "schema_name": "sales",
            "max_results": 25,
            "page_token": "opaque-previous",
        },
    )
    assert page.json()["next_page_token"] == "opaque-next"
    assert calls[-1].url.params["page_token"] == "opaque-previous"
    assert (
        client.get("/api/catalog/tables/retail.sales.orders").json()["columns"][0][
            "name"
        ]
        == "id"
    )
    assert (
        client.get(
            "/api/catalog/tables",
            params={
                "catalog_name": "retail",
                "schema_name": "sales",
                "max_results": 51,
            },
        ).status_code
        == 422
    )


def test_catalog_validates_names_columns_and_locations_before_forwarding(
    client, monkeypatch
):
    calls = []
    mock_catalog(
        monkeypatch,
        lambda request: calls.append(request) or httpx.Response(200, json={}),
    )
    assert (
        client.post("/api/catalog/catalogs", json={"name": "../escape"}).status_code
        == 400
    )
    assert client.get("/api/catalog/tables/not.three").status_code == 400
    base = {
        "name": "orders",
        "catalog_name": "retail",
        "schema_name": "sales",
        "data_source_format": "PARQUET",
        "storage_location": "s3://warehouse/orders",
        "columns": [{"name": "id", "type_name": "INT", "type_text": "int"}],
    }
    invalids = [
        {**base, "storage_location": "s3://key:secret@bucket/orders"},
        {**base, "storage_location": "file:///data/../escape"},
        {**base, "data_source_format": "EXECUTABLE"},
        {
            **base,
            "columns": [
                {"name": "id", "type_name": "STRUCT", "type_text": "struct<x:int>"}
            ],
        },
        {
            **base,
            "columns": [
                {
                    "name": "id",
                    "type_name": "INT",
                    "type_text": "int",
                    "type_json": "not json",
                }
            ],
        },
        {
            **base,
            "columns": [
                {"name": "id", "type_name": "INT", "type_text": "int", "position": 1}
            ],
        },
        {**base, "columns": [base["columns"][0], base["columns"][0]]},
    ]
    for payload in invalids:
        assert client.post("/api/catalog/tables", json=payload).status_code == 400
    assert not calls


def test_catalog_member_read_access_admin_create_and_safe_upstream_errors(
    client, monkeypatch
):
    mock_catalog(
        monkeypatch, lambda request: httpx.Response(200, json={"catalogs": []})
    )
    client.post(
        "/api/users",
        json={
            "username": "reader",
            "password": "test-reader-password",
            "role": "member",
        },
    ).raise_for_status()
    client.post("/api/auth/logout").raise_for_status()
    client.post(
        "/api/auth/login",
        json={"username": "reader", "password": "test-reader-password"},
    ).raise_for_status()
    assert client.get("/api/catalog/catalogs").status_code == 200
    assert (
        client.post("/api/catalog/catalogs", json={"name": "retail"}).status_code == 403
    )
    mock_catalog(
        monkeypatch,
        lambda request: httpx.Response(
            401, json={"message": "secret token test-secret-value"}
        ),
    )
    response = client.get("/api/catalog/catalogs")
    assert response.status_code == 502 and "test-secret-value" not in response.text
    assert not client.get("/api/catalog/status").json()["available"]


def test_catalog_client_keeps_optional_token_out_of_url(monkeypatch):
    monkeypatch.setenv(
        "OPENBRICKS_CATALOG_URL", "https://catalog.example/api/2.1/unity-catalog/"
    )
    monkeypatch.setenv("OPENBRICKS_CATALOG_TOKEN", "private-token-value")
    assert catalog.catalog_url() == "https://catalog.example/api/2.1/unity-catalog"
    with catalog.catalog_client(catalog.catalog_url()) as client:
        assert client.headers["Authorization"] == "Bearer private-token-value"
        assert "private-token-value" not in str(client.base_url)
