# Databricks notebook source
# MAGIC %md
# MAGIC # Hello, OpenBricks
# MAGIC This notebook uses real local Spark. Import this file from Workspace.

# COMMAND ----------

sales = spark.createDataFrame(
    [("North", "Notebook", 12, 8.50), ("South", "Pencil", 30, 1.25),
     ("North", "Pencil", 18, 1.25), ("West", "Notebook", 9, 8.50)],
    ["region", "product", "quantity", "unit_price"],
)
sales.createOrReplaceTempView("sales")
display(sales)

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT region,
# MAGIC        SUM(quantity) AS units,
# MAGIC        ROUND(SUM(quantity * unit_price), 2) AS revenue
# MAGIC FROM sales
# MAGIC GROUP BY region
# MAGIC ORDER BY revenue DESC

# COMMAND ----------

sales.write.mode("overwrite").saveAsTable("sample_sales")
print("Created sample_sales in the local persistent Spark warehouse.")
