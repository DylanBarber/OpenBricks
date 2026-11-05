# Data workspace, quality, lineage, and Delta

The data workspace runs SQL, previews, quality checks, Delta maintenance, and incremental imports on the compute service you select. The management server stores project metadata and queues work; it does not start a Spark session to answer these requests.

Choose a project and compute service in the UI. Project viewers can browse saved metadata/results, operators can run work, editors can maintain query/import definitions, and owners can manage destructive maintenance. Connection assignment and cluster assignment still apply. These application permissions organize a trusted team's work; they do not isolate arbitrary notebook/SQL code from the compute account's underlying data privileges.

## SQL and data explorer

Use **Data workspace** to select Spark or a saved Microsoft SQL Server connection, browse schemas/tables/columns, preview rows, and save queries. Spark's local `spark_catalog` works without Unity Catalog. Configured Unity Catalog catalogs are addressed by their catalog names.

Queries and previews return at most 1,000 rows. The default preview is 100 rows; strings are bounded to 10,000 characters and the preview payload to approximately 2 MB. A truncated preview is a sample, not a complete export. Charts use that preview. Query history is paged separately from saved queries and retains the selected project, compute service, execution ID, and underlying notebook run ID.

Both engines accept positional `?` parameters with a JSON parameter array. Spark binds them through `SparkSession.sql(..., args=[...])`; SQL Server uses ODBC parameter binding. A query with supplied editor SQL runs that SQL even when associated with a saved query; it does not silently discard unsaved editor changes. Supplying only a saved `query_id` loads its saved SQL and defaults.

Metadata operations support `offset` and return `next_offset`. SQL Server uses ordered `OFFSET/FETCH` queries; Spark sorts the metadata frame before applying the offset. Offsets are for schema/table/column browsing, rather than an unlimited query-result download.

## Native SQL Server reads for larger datasets

The existing `query`, `read`, and `to_spark` helpers are bounded conveniences. For an actual Spark JDBC read, use `read_spark`:

```python
sql = connections.sqlserver("Warehouse")  # Explicitly select this connection for the notebook.
orders = sql.read_spark(
    spark,
    "dbo.Orders",
    partition_column="OrderId",
    lower_bound=0,
    upper_bound=10_000_000,
    num_partitions=8,
    fetch_size=10_000,
)
display(orders.limit(100))
```

This returns a distributed JDBC DataFrame without first collecting the dataset into Python or imposing the Python helper's row cap. The configured credentials are JDBC properties, not embedded in its URL. The compute image includes Microsoft's JDBC driver.

Bounds determine partition strides; they do **not** filter out rows beyond those bounds. To filter, supply a source query or predicates. Use predicates instead of numeric partition options:

```python
recent = sql.read_spark(
    spark,
    query="SELECT OrderId, UpdatedAt, Amount FROM dbo.Orders WHERE IsDeleted = 0",
    predicates=["OrderId < 100000", "OrderId >= 100000"],
    fetch_size=5000,
)
data.lineage("sqlserver://CONNECTION_ID/dbo/Orders", "read", {"method": "jdbc"})
```

At most 256 partitions/predicates are allowed. Choose a sensible concurrency for the source database. JDBC availability is verified through contract tests and driver packaging; a live SQL Server round trip requires a reachable SQL Server endpoint and credentials.

## Quality checks that can fail a pipeline

```python
orders = data.read({"path": "blob://silver/orders", "format": "delta"})
report = data.quality(orders, [
    {"type": "not_null", "column": "OrderId"},
    {"type": "unique", "column": "OrderId"},
    {"type": "range", "column": "Amount", "min": 0, "max": 1_000_000},
    {"type": "row_count", "min": 1},
    {"type": "freshness", "column": "UpdatedAt", "max_age_seconds": 86400},
], dataset="blob://silver/orders")
```

Checks run over the Spark DataFrame rather than its preview. The report includes the total rows, individual checks, failed-row counts, and overall pass/fail. It is emitted before a `DataQualityError` fails the cell/run by default, so a failed pipeline still has its quality report. Use `fail_on_error=False` when investigating data without stopping the notebook. Ingestion quality checks always stop a bad batch before its sink write.

Uniqueness counts excess duplicate rows, including duplicate nulls; combine it with a not-null check when null keys are forbidden. Range checks include null values as failures. Freshness compares the maximum timestamp with the current UTC time; it checks dataset freshness, not the age of every row. Row-count/range checks need a minimum or maximum.

## Dataset lineage

`data.read`, `data.write`, Delta reads/restores, and incremental ingestion emit dataset read/write events tied to the run and project. SQL workspace queries also infer simple table references; that inference is intentionally best-effort and does not fully parse dynamic SQL, views, or every SQL dialect.

For accurate lineage in custom notebook code, use the helpers or explicitly record a dataset:

```python
data.write(orders, {"path": "blob://gold/orders", "format": "delta"}, mode="overwrite")
data.lineage("spark_catalog.gold.orders", "write", {"purpose": "daily reporting"})
```

Arbitrary `spark.read`, `DataFrame.write`, JDBC, or Python filesystem calls are not globally intercepted. Lineage events from failed runs remain useful observations and link back to those runs; an event alone does not certify that an entire pipeline succeeded. Remote polling replays events idempotently without creating duplicate lineage rows.

## Delta management without Unity Catalog

Delta extensions and the packaged Delta jars load independently of the optional Unity Catalog URL. Paths can use local `blob://` storage, or object storage configured for the selected Spark service. Table identifiers accept simple one-, two-, or three-part names.

```python
display(data.delta.history(path="blob://silver/orders", limit=20))
display(data.delta.detail(path="blob://silver/orders"))
display(data.delta.read(path="blob://silver/orders", version=0).limit(100))

# Preview obsolete files first. This does not delete them.
display(data.delta.vacuum(path="blob://silver/orders", retention_hours=168))
```

Restore and actual vacuum are explicit operations in the UI/API:

```python
display(data.delta.restore(path="blob://silver/orders", version=2))
display(data.delta.vacuum(path="blob://silver/orders", retention_hours=168, dry_run=False))
```

Vacuum requires at least 168 hours of retention. The helpers do not disable Delta's retention safety check. Vacuum permanently removes files and may make old versions unavailable; restore requires the referenced files to remain. Time travel accepts either a version or timestamp. Python/SQL notebook authors remain trusted and can execute engine commands directly with the compute account's privileges.

## Incremental SQL Server → Delta imports

### Change Tracking mode

Choose **Change Tracking** for synchronization of current rows, including inserts, updates, and deletions. It uses SQL Server Change Tracking rather than CDC capture tables. The source needs a primary key, database snapshot isolation, and Change Tracking enabled on the database and table. Ask the database administrator to configure retention longer than the longest expected ingestion outage. For example, on a disposable database:

```sql
ALTER DATABASE Example SET ALLOW_SNAPSHOT_ISOLATION ON;
ALTER DATABASE Example SET CHANGE_TRACKING = ON
    (CHANGE_RETENTION = 7 DAYS, AUTO_CLEANUP = ON);
ALTER TABLE dbo.Orders ENABLE CHANGE_TRACKING;
```

Grant the connector account the required read and `VIEW CHANGE TRACKING` permissions. Merge keys must match the complete source primary key. Use a new Delta sink owned by this ingestion; an unrelated pre-existing table is rejected.

Text primary keys (`char`, `varchar`, `nchar`, `nvarchar`, and `sysname`) are rejected before staging any data. SQL Server collation rules and its treatment of trailing spaces differ from Spark string equality, including with BIN2 collations. Key spelling changes could otherwise create duplicate or undeleted sink rows. Use a supported non-text source primary key for Change Tracking. Text columns outside the primary key remain supported, and ordinary SQL queries and JDBC reads still support text keys.

Native SQL queries return `time` and `datetimeoffset` as ISO strings, preserving SQL Server's seven fractional digits and the original timezone offset. Change Tracking stores `time` as text because Spark does not support the Parquet TIME type. Spark timestamp columns have microsecond precision; `datetimeoffset` values normalize to UTC, and a seventh fractional digit on a non-key timestamp is narrowed to microseconds. Timestamp primary keys with precision greater than six are rejected before writing because distinct SQL keys could otherwise collapse to the same Spark timestamp.

```json
{
  "project_id": "default",
  "name": "Orders synchronization",
  "mode": "change_tracking",
  "connection_id": "YOUR_SQL_SERVER_CONNECTION_ID",
  "source": {"schema": "dbo", "table": "Orders"},
  "sink": {"path": "blob://silver/orders_sync"},
  "keys": ["OrderId"],
  "jdbc": {"fetch_size": 4096},
  "checks": []
}
```

The first run snapshots the source. Later runs read `CHANGETABLE` and current values inside one snapshot transaction, stream bounded batches through temporary Parquet, and merge inserts/updates/deletes into Delta. Checkpoints advance only after the whole run succeeds. Replaying an interrupted batch is idempotent by primary key. Initial replay can reconcile missing source rows only within the sink owned by this ingestion.

Source identity, column/type/length/nullability/collation, and primary-key changes are checked before writing. An expired checkpoint, source reset, or schema change stops the run. Review the source and create a new ingestion with a new sink for a fresh snapshot; the application does not silently discard a checkpoint or evolve a schema. Unsupported source types fail explicitly. Parallel JDBC partition options are unavailable in this mode because the read uses one snapshot connection. Change Tracking does not preserve every intermediate row version or historical event.

**Upgrading from the 0.4 beta:** stable 0.4.0 includes collation in the schema fingerprint. Existing beta Change Tracking checkpoints fail closed because they use the older fingerprint. Review the source key requirements, then explicitly create a new ingestion with a new dedicated Delta sink for a fresh snapshot and validate it before switching consumers. Keep the old ingestion paused and its sink available until validation is complete. Do not edit or clear checkpoints manually, or reuse the old sink to bypass validation.

### Watermark mode

Choose **Watermark** and create an import definition with a SQL Server connection, source schema/table, Delta sink path or table, merge keys, and watermark column/type. The first run reads after an optional initial watermark; later runs read strictly greater than the last successful watermark. Numeric, timestamp, date, and string watermarks are supported.

The source must have non-null merge keys and watermark values, with one current row per key in a batch. The sink uses Delta `MERGE` to update matching keys and insert new keys. Quality checks run before writing. The new watermark is considered only after the sink succeeds and is committed to the saved definition only after the **entire run** succeeds. A later cell failure, cancellation, sink error, or quality failure leaves the prior watermark unchanged. A retry can re-read the batch; key-based merge makes replay idempotent.

Only one queued/running execution of a saved import is allowed. Its definition cannot be edited/deleted while active. Once an import has succeeded, create a new definition to change its source, sink, merge keys, or watermark; changing these while keeping an unrelated checkpoint could silently skip data. Fetch size and partition tuning can be changed separately.

This is batch watermark ingestion, not SQL Server CDC. It does not propagate source deletions or automatically capture late-arriving records at or below the committed watermark. Use a monotonically advancing update column and appropriate source consistency. Parallel JDBC reads are separate source connections, so this does not promise a transactionally consistent cross-partition snapshot or exactly-once source delivery. Local blob paths/metastores belong to the selected compute service; use shared configured storage when multiple servers need the same sink.

## HTTP API

### DuckDB file queries

In the SQL workspace choose **DuckDB**, then add a dataset alias, a `blob://bucket/file` path, and CSV, Parquet, JSON, or JSONL format. Select an assigned S3 connection for `s3://bucket/key` sources. Query the aliases with ordinary SQL:

```sql
SELECT customer, sum(amount) AS total
FROM orders
GROUP BY customer
ORDER BY total DESC
```

The source list is saved with the query in `payload.sources`:

```json
{"sources": [{"name": "orders", "path": "blob://bronze/orders.csv", "format": "csv"}]}
```

DuckDB runs inside a budgeted job worker without starting Spark. Local sources are confined to local buckets; S3 objects are staged with the selected connection, bounded to 256 MB per object. Memory/threads and result previews are bounded, and extension installation/loading is disabled. Use Spark for larger distributed datasets and Delta operations. Trusted SQL authors still execute with the compute account's privileges; this is not an SQL sandbox for hostile queries.

All paths below begin `/api/data` and require login/project access. List responses are `{items,total,limit,offset}`; `limit` defaults to 25 and is capped at 100 for stored metadata.

| Method/path | Contract |
| --- | --- |
| `GET /queries?project_id=&search=&limit=&offset=` | Saved queries in the selected project |
| `POST /queries`, `PUT /queries/{id}` | `{project_id,name,description?,engine,sql,params:[],connection_id?,cluster_id?,payload:{sources:[]}}` |
| `DELETE /queries/{id}` | Delete saved query, preserving execution history |
| `POST /execute` | `{project_id,cluster_id?,query_id?,operation,engine,connection_id?,sql?,params:[],catalog?,schema?,table?,limit:100,offset:0,payload:{}}` → execution metadata with `id` and `run_id` (202) |
| `GET /executions?project_id=&query_id=&limit=&offset=` | Paged execution metadata |
| `GET /executions/{id}` | Metadata plus current status, `result`, `quality_reports`, `lineage`, and error |
| `GET /lineage?project_id=&dataset=&limit=&offset=` | Paged dataset observations linked to runs |
| `GET /ingestions?project_id=&limit=&offset=` | Saved import definitions/checkpoints |
| `POST /ingestions`, `PUT /ingestions/{id}` | Definition shown below |
| `POST /ingestions/{id}/run` | Optional `{cluster_id}` override → queued execution (202) |
| `DELETE /ingestions/{id}` | Delete an inactive definition; leave sink data intact |

Operations are `query`, `catalogs` (Spark), `schemas`, `tables`, `columns`, `preview`, `quality`, `delta_history`, `delta_detail`, `delta_preview`, `delta_restore`, `delta_vacuum`, and `ingest`. Results use `{columns,rows,row_count,truncated}`; metadata adds `offset,next_offset`. Quality also includes its report. Delta payloads accept exactly one `path`/`table`, plus `version`/`timestamp`, `retention_hours`, or `dry_run` as applicable. Quality payloads are `{dataset:{path,format} OR {table},checks:[],fail_on_error:true}`.

Example saved ingestion definition (replace the connection ID and source names):

```json
{
  "project_id": "default",
  "name": "Orders to silver",
  "connection_id": "YOUR_SQL_SERVER_CONNECTION_ID",
  "cluster_id": "local",
  "source": {"schema": "dbo", "table": "Orders"},
  "sink": {"path": "blob://silver/orders"},
  "keys": ["OrderId"],
  "watermark": {"column": "UpdatedAt", "type": "timestamp", "initial": "2026-01-01T00:00:00"},
  "jdbc": {"fetch_size": 10000},
  "checks": [{"type": "not_null", "column": "OrderId"}]
}
```

Notebook access to named connections is explicit: assign the connection to the project and select its ID for that notebook/task. An empty selection dispatches no credentials, including for an administrator's task. Administrators can manage/test connections globally, but task dispatch still requires project assignment.

## Validation

Protocol/permission tests cover paging, parameter/options handling, strict credential selection, explicit Delta maintenance, duplicate event replay, saved-query editor changes, ingestion overlap protection, and checkpoint advancement only after full success. Enable `OPENBRICKS_TEST_SPARK=1` in the packaged Java 17/Spark test environment for real DataFrame checks. The opt-in [SQL Server test harness](../compose.sql-tests.yaml) creates and removes a uniquely named test database. Its real ODBC/JDBC tests cover parameter binding, transactional rollback, more than 12,000 rows, Change Tracking insert/update/delete replay into Delta, schema drift, sink ownership, and invalid checkpoints. Run it only against disposable test infrastructure; see [verification](../VERIFICATION.md).

The native JDBC behavior follows [Spark's JDBC documentation](https://spark.apache.org/docs/4.0.1/sql-data-sources-jdbc.html). Driver packaging uses [Microsoft JDBC 13.6.0 for Java 11+](https://learn.microsoft.com/en-us/sql/connect/jdbc/release-notes-for-the-jdbc-driver?view=sql-server-ver17); Delta 4.3.1 matches the existing Spark 4.0 jars and [Delta's Spark compatibility guidance](https://docs.delta.io/releases/).
