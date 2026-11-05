-- Run 00_run_pipeline.py once on this compute service before querying.
-- The table survives interpreter restarts; it belongs to the local Spark metastore.
SELECT order_date, region, orders, units, revenue
FROM spark_catalog.openbricks_demo.daily_revenue
ORDER BY order_date, region
