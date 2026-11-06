# Dashboards and data assets

OpenBricks dashboards reuse project-scoped saved SQL queries. Reading a dashboard
uses a saved result; it never starts Spark or submits a database query. An
operator can refresh a dashboard, and an editor can change its definition.

## Create a dashboard

Save a SELECT query in **Data workspace**, then choose it when adding a tile in
**Dashboards**. Tiles support a table, bar chart, line chart or single value.
Set column names for chart labels and values, and choose a tile width/height.
Layouts and chart configuration persist with the dashboard.

Dashboard filters use the same typed definitions as notebook parameters. A tile's
`parameter_bindings` maps positional SQL parameter indexes to filter names:

```json
{
  "project_id": "default",
  "name": "Daily orders",
  "parameters": [{"name": "minimum", "type": "integer", "default": 10}],
  "tiles": [{
    "id": "orders", "title": "Orders", "query_id": "SAVED_QUERY_ID",
    "chart": "bar", "x": "product", "y": "revenue",
    "width": 6, "height": 4, "parameter_bindings": {"0": "minimum"}
  }],
  "cron": "0 8 * * *",
  "timezone": "America/New_York"
}
```

For example, the saved query might be `SELECT product, revenue FROM orders WHERE
revenue >= ?`. Values are bound as parameters, never interpolated into SQL. Every
filter bound to a tile needs a supplied value or default. Scheduled refreshes need
defaults for their filters. Basic and Advanced schedule controls use the same
five-field cron/timezone semantics as notebook jobs.

Tiles accept one SELECT/WITH statement and reject explicit writes and multiple
statements. This is a conservative convenience check, not a SQL or arbitrary-code
sandbox. Underlying database credentials should have the access the query needs.

## Refresh and cache behavior

Refresh returns a durable queued record immediately. The management coordinator
submits each tile through the existing data workspace/compute path, one tile at a
time per dashboard. It rechecks the run-as account, saved query revision, project
membership, connection assignments and compute access before dispatch. A changed
or removed query stops a pending refresh and requires a new one. Stored snapshots
show the query hash/revision and run IDs used to create them.

A complete successful refresh publishes its snapshot. A failed or interrupted
refresh preserves the previous successful snapshot and shows its timestamp plus
the latest refresh failure. A run that is uncertain after a management restart is
never automatically retried. Submitted runs with known IDs can be reconciled;
pending tiles may continue after their permissions are checked again.

Different resolved filter values have separate caches. Reading uncached values
returns 404 with an instruction to ask an operator to refresh. Viewers do not
acquire query-execution permission through dashboard access. A stale badge means
the snapshot exceeds its TTL (one hour by default), or the latest refresh for
those values failed. It does not mean the cache was recomputed on read.

Results are deliberately bounded: at most 12 tiles, 1,000 rows and 200 columns per
tile, 10,000 characters per string, and 2 MB for the complete snapshot. Charts
visualize these bounded query results, not an automatic scan of all source rows.
Aggregate in SQL when a chart needs totals over a large dataset.

Each dashboard retains at most ten recent filter caches and 100 refresh-history
records; active refreshes are preserved. A separate 64 MiB workspace-wide budget
evicts older dashboard caches. An evicted cache can be refreshed again. These
caches are metadata-volume artifacts and are included in workspace backups.
Changing a dashboard definition creates a new generation, so old layouts/results
are not silently displayed for a changed definition.

Schedules run as the last editor who saved the dashboard. Expired/revoked
automation tokens and disabled or removed accounts cannot dispatch new work.
Scheduled firings during maintenance or an already-active refresh are skipped;
dashboard schedules do not promise the missed-run replay provided by notebook
automation. Backup/recovery drill mode does not run dashboard schedules.

## Asset health and lineage

**Data assets** organizes recorded dataset observations into a searchable,
paginated asset list. An editor can add a description, assign an active project
member as owner, and set a freshness threshold. Readers see the same project
boundaries as notebooks and query results.

Health combines the latest observed quality report with recorded successful
writes. Freshness is explicitly the age of the last **recorded successful write**,
not a live inspection of source rows. The quality panel shows the observed run,
run status, check results and measurement time. A failed run can leave useful
quality evidence; an observation does not certify that all of its external
effects committed as one transaction.

The graph infers a dataset edge when one run records a read of one dataset and a
write of another. Each edge links to its observed run and status. This is
dataset-level, same-run inference; arbitrary Spark/JDBC/Python reads and writes
are not globally intercepted. Use the existing `data.read`, `data.write` and
`data.lineage` helpers for the observations that matter.

Graph requests are bounded to two hops, 100 nodes and 500 observed edges. Quality
views inspect at most the latest 500 project runs and 1,000 matching reports;
per-asset write/read summaries use the latest 100 observations. Responses expose
these evidence limits and graph truncation. Older evidence can remain in run
history without appearing in a bounded health view. No asset GET starts compute.

## API contract

All routes require login and project access. Lists use `{items,total,limit,offset}`.

| Route | Access and behavior |
| --- | --- |
| `GET /api/dashboards?project_id=...` | Read; summary list with search and pagination |
| `GET /api/dashboards/query-options?project_id=...` | Read; paginated query picker metadata without SQL, parameter values or source payloads |
| `POST /api/dashboards`, `PUT /api/dashboards/{id}` | Edit; the definition above, plus optional `description`, `enabled`, `cache_ttl_seconds` |
| `GET /api/dashboards/{id}` | Read; definition and latest refresh metadata |
| `DELETE /api/dashboards/{id}` | Edit; requires no active refresh |
| `POST /api/dashboards/{id}/refresh` | Run; `{parameters:{...}}` → 202 queued refresh |
| `GET /api/dashboards/{id}/refreshes` | Read; paginated metadata history |
| `GET /api/dashboards/{id}/refreshes/{refresh_id}` | Read; tile status and run/execution IDs |
| `GET /api/dashboards/{id}/snapshot?parameters={...}` | Read; matching last successful cache, age/stale state and latest refresh metadata; 404 when uncached |
| `GET /api/assets?project_id=...` | Read; paginated/searchable asset health |
| `GET /api/assets/graph?project_id=...&dataset=...` | Read; optional `direction=both|upstream|downstream`, `depth=1|2`, `max_nodes<=100` |
| `GET /api/assets/{id}?project_id=...` | Read; one asset's observed health |
| `GET /api/assets/{id}/quality?project_id=...` | Read; paginated recorded quality reports within the documented evidence window |
| `PUT /api/assets/{id}` | Edit; `{project_id,dataset,description,owner_id,freshness_seconds}` |

Asset IDs are the SHA-256 of the compact JSON array `[project_id,dataset]`.
Dataset URIs cannot contain embedded credentials, query strings or fragments.
Dashboard responses do not expose saved SQL, connection credentials or internal
dispatch snapshots. Errors link callers to existing run diagnostics rather than
copying raw driver error text into cached dashboards.
