"""Authenticated management API for the optional OSS Unity Catalog service.

The wire contract follows Unity Catalog 0.7.0's published OpenAPI schema.
This module registers metadata; Spark/storage readers remain responsible for
accessing the files at an external table's storage location.
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import quote, unquote, urlsplit

import httpx
from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field

API_PREFIX = "/api/2.1/unity-catalog"
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,254}$")
TYPES = {"BOOLEAN", "BYTE", "SHORT", "INT", "LONG", "FLOAT", "DOUBLE", "DATE", "TIMESTAMP", "TIMESTAMP_NTZ", "STRING", "BINARY", "DECIMAL", "ARRAY", "STRUCT", "MAP", "VARIANT", "CHAR", "NULL", "INTERVAL"}
PRIMITIVE_JSON = {"BOOLEAN": "boolean", "BYTE": "byte", "SHORT": "short", "INT": "integer", "LONG": "long", "FLOAT": "float", "DOUBLE": "double", "DATE": "date", "TIMESTAMP": "timestamp", "TIMESTAMP_NTZ": "timestamp_ntz", "STRING": "string", "BINARY": "binary", "NULL": "void", "VARIANT": "variant"}


class CatalogInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    comment: str | None = Field(default=None, max_length=4000)


class SchemaInput(CatalogInput):
    catalog_name: str = Field(min_length=1, max_length=255)


class ColumnInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type_name: str = Field(min_length=1, max_length=40)
    type_text: str = Field(min_length=1, max_length=4000)
    type_json: str | None = Field(default=None, max_length=20000)
    position: int | None = Field(default=None, ge=0, le=1999)
    nullable: bool = True


class TableInput(SchemaInput):
    schema_name: str = Field(min_length=1, max_length=255)
    data_source_format: str = "PARQUET"
    storage_location: str = Field(min_length=1, max_length=4000)
    columns: list[ColumnInput] = Field(min_length=1, max_length=2000)


def identifier(value: str):
    if not IDENTIFIER.fullmatch(value):
        raise ValueError("Catalog, schema, table, and column names must begin with a letter or underscore and contain only letters, numbers, and underscores")
    return value


def catalog_url():
    value = os.getenv("OPENBRICKS_CATALOG_URL", "").strip().rstrip("/")
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("OPENBRICKS_CATALOG_URL must be an HTTP(S) URL without embedded credentials, a query, or fragment")
    try:
        parsed.port
    except ValueError:
        raise ValueError("OPENBRICKS_CATALOG_URL has an invalid port")
    return value if parsed.path.rstrip("/").endswith(API_PREFIX) else value + API_PREFIX


def catalog_client(base_url):
    token = os.getenv("OPENBRICKS_CATALOG_TOKEN", "").strip()
    headers = {"Authorization": "Bearer " + token} if token else {}
    verify = os.getenv("OPENBRICKS_CATALOG_VERIFY_TLS", "true").lower() == "true"
    return httpx.Client(base_url=base_url + "/", headers=headers, verify=verify, timeout=httpx.Timeout(15, connect=4), follow_redirects=False)


def catalog_request(method, path, *, params=None, body=None):
    try:
        url = catalog_url()
    except ValueError:
        raise HTTPException(503, "Unity Catalog URL is invalid. Check the server configuration")
    if not url:
        raise HTTPException(503, "Unity Catalog is not configured. Enable the catalog service or set OPENBRICKS_CATALOG_URL")
    try:
        with catalog_client(url) as client:
            response = client.request(method, path.lstrip("/"), params=params, json=body)
    except httpx.HTTPError:
        raise HTTPException(502, "Unity Catalog is unavailable. Check that its service is running and reachable")
    if not response.is_success:
        if response.status_code in (401, 403):
            raise HTTPException(502, "Unity Catalog authentication or authorization failed. Check its server token and permissions")
        if response.status_code == 404:
            raise HTTPException(404, "Catalog object not found")
        if response.status_code == 409:
            raise HTTPException(409, "A catalog object with this name already exists")
        if response.status_code in (400, 422):
            raise HTTPException(400, "Unity Catalog rejected the metadata. Check the names, columns, format, and storage location")
        raise HTTPException(502, f"Unity Catalog request failed (HTTP {response.status_code})")
    try:
        result = response.json()
    except ValueError:
        raise HTTPException(502, "Unity Catalog returned an invalid response")
    if not isinstance(result, dict):
        raise HTTPException(502, "Unity Catalog returned an invalid response")
    return result


def collection_response(result, key):
    items = result.get(key, [])
    if not isinstance(items, list):
        raise HTTPException(502, "Unity Catalog returned an invalid collection")
    return {"items": items, "next_page_token": result.get("next_page_token")}


def storage_location(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ("file", "s3", "s3a", "abfss", "gs") or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use a file://, s3://, s3a://, abfss://, or gs:// storage URI without embedded credentials")
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost") or not parsed.path.startswith("/"):
            raise ValueError("Local table locations use an absolute file:/// path")
    elif not parsed.netloc:
        raise ValueError("Storage location must identify a bucket or storage account")
    if ".." in unquote(parsed.path).replace("\\", "/").split("/") or "\x00" in value:
        raise ValueError("Storage location cannot contain parent directory traversal")
    return value


def table_payload(body: TableInput):
    identifier(body.name)
    identifier(body.catalog_name)
    identifier(body.schema_name)
    format_name = body.data_source_format.upper()
    if format_name not in ("PARQUET", "DELTA", "CSV", "JSON"):
        raise ValueError("Supported table formats are PARQUET, DELTA, CSV, and JSON")
    columns, names = [], set()
    for index, column in enumerate(body.columns):
        identifier(column.name)
        if column.name.casefold() in names:
            raise ValueError("Column names must be unique")
        names.add(column.name.casefold())
        type_name = column.type_name.upper()
        if type_name not in TYPES:
            raise ValueError("Unsupported Unity Catalog column type")
        position = index if column.position is None else column.position
        if position != index:
            raise ValueError("Column positions must match their zero-based order")
        type_json = column.type_json
        if type_json:
            try:
                decoded = json.loads(type_json)
            except ValueError:
                raise ValueError("Column type_json must contain valid JSON")
            if not isinstance(decoded, dict) or decoded.get("name") != column.name or "type" not in decoded:
                raise ValueError("Column type_json must describe that column's name and type")
        else:
            json_type = PRIMITIVE_JSON.get(type_name)
            if type_name == "DECIMAL":
                match = re.fullmatch(r"decimal\(\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)", column.type_text, flags=re.I)
                if match and 1 <= int(match[1]) <= 38 and 0 <= int(match[2]) <= int(match[1]):
                    json_type = f"decimal({int(match[1])},{int(match[2])})"
            if not json_type:
                raise ValueError("Provide type_json for complex column types")
            type_json = json.dumps({"name": column.name, "type": json_type, "nullable": column.nullable, "metadata": {}})
        columns.append({"name": column.name, "type_name": type_name, "type_text": column.type_text,
                        "type_json": type_json, "position": position, "nullable": column.nullable})
    return {"name": body.name, "catalog_name": body.catalog_name, "schema_name": body.schema_name, "table_type": "EXTERNAL",
            "data_source_format": format_name, "storage_location": storage_location(body.storage_location), "columns": columns,
            **({"comment": body.comment} if body.comment is not None else {})}


def register_catalog_routes(app, authenticated, admin):
    @app.get("/api/catalog/status")
    def status(user=Depends(authenticated)):
        try:
            url = catalog_url()
        except ValueError:
            return {"configured": True, "available": False, "error": "The catalog server URL is invalid"}
        if not url:
            return {"configured": False, "available": False}
        try:
            catalog_request("GET", "/catalogs", params={"max_results": 1})
        except HTTPException as exc:
            return {"configured": True, "available": False, "url": url.removesuffix(API_PREFIX), "error": exc.detail}
        return {"configured": True, "available": True, "url": url.removesuffix(API_PREFIX)}

    @app.get("/api/catalog/catalogs")
    def catalogs(max_results: int = Query(default=100, ge=1, le=1000), page_token: str | None = Query(default=None, max_length=4000), user=Depends(authenticated)):
        params = {"max_results": max_results, **({"page_token": page_token} if page_token else {})}
        return collection_response(catalog_request("GET", "/catalogs", params=params), "catalogs")

    @app.post("/api/catalog/catalogs", status_code=201)
    def create_catalog(body: CatalogInput, user=Depends(admin)):
        identifier(body.name)
        return catalog_request("POST", "/catalogs", body=body.model_dump(exclude_none=True))

    @app.get("/api/catalog/schemas")
    def schemas(catalog_name: str, max_results: int = Query(default=100, ge=1, le=1000), page_token: str | None = Query(default=None, max_length=4000), user=Depends(authenticated)):
        identifier(catalog_name)
        params = {"catalog_name": catalog_name, "max_results": max_results, **({"page_token": page_token} if page_token else {})}
        return collection_response(catalog_request("GET", "/schemas", params=params), "schemas")

    @app.post("/api/catalog/schemas", status_code=201)
    def create_schema(body: SchemaInput, user=Depends(admin)):
        identifier(body.name)
        identifier(body.catalog_name)
        return catalog_request("POST", "/schemas", body=body.model_dump(exclude_none=True))

    @app.get("/api/catalog/tables")
    def tables(catalog_name: str, schema_name: str, max_results: int = Query(default=25, ge=1, le=50), page_token: str | None = Query(default=None, max_length=4000), user=Depends(authenticated)):
        identifier(catalog_name)
        identifier(schema_name)
        params = {"catalog_name": catalog_name, "schema_name": schema_name, "max_results": max_results, **({"page_token": page_token} if page_token else {})}
        return collection_response(catalog_request("GET", "/tables", params=params), "tables")

    @app.get("/api/catalog/tables/{full_name}")
    def table(full_name: str, user=Depends(authenticated)):
        parts = full_name.split(".")
        if len(parts) != 3:
            raise ValueError("Use a three-part catalog.schema.table name")
        for part in parts:
            identifier(part)
        return catalog_request("GET", "/tables/" + quote(full_name, safe="."))

    @app.post("/api/catalog/tables", status_code=201)
    def create_table(body: TableInput, user=Depends(admin)):
        return catalog_request("POST", "/tables", body=table_payload(body))
