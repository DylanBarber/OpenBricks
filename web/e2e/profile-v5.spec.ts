import { test, expect, createNotebook } from "./helpers";

type RecordedRun = {
  id: string;
  status: string;
  session_id?: string;
  error?: string;
};

test("real Spark run profiler renders charts, stages, plans, and a previous-run comparison", async ({
  admin,
  api,
  prefix,
}) => {
  test.setTimeout(240_000);
  const name = `${prefix}-profile`;
  const notebook = await createNotebook(api, name, [
    "display(spark.range(1000).selectExpr('id % 5 AS grouping').groupBy('grouping').count())",
  ]);
  let current: RecordedRun | undefined;
  try {
    const response = await api.post(`/api/notebooks/${notebook.id}/run`, {
      data: { resource_options: { mode: "job", memory_mb: 1536 } },
    });
    expect(response.ok()).toBeTruthy();
    const previous = (await response.json()) as RecordedRun;
    await expect
      .poll(
        async () => {
          const value = (await (
            await api.get(`/api/runs/${previous.id}`)
          ).json()) as RecordedRun;
          return value.status;
        },
        { timeout: 180_000, intervals: [500, 1000] },
      )
      .toBe("succeeded");

    await admin.reload();
    await admin
      .getByRole("button", { name: `Open ${name}`, exact: true })
      .click();
    await admin.getByRole("button", { name: "Run all", exact: true }).click();
    await expect
      .poll(
        async () => {
          const value = (await (
            await api.get(
              `/api/history/runs?notebook_id=${notebook.id}&limit=5`,
            )
          ).json()) as { items: RecordedRun[] };
          current = value.items.find((run) => run.id !== previous.id);
          return current?.status;
        },
        { timeout: 180_000, intervals: [500, 1000] },
      )
      .toBe("succeeded");

    const profiler = admin.getByTestId("run-profiler");
    await profiler.getByText("Run profiler", { exact: true }).click();
    await expect(
      profiler.getByRole("img", { name: "Memory over time" }),
    ).toBeVisible();
    await expect(
      profiler.getByRole("img", { name: "CPU over time" }),
    ).toBeVisible();
    await expect(
      profiler.getByRole("table", {
        name: "Spark stage timings and data movement",
      }),
    ).toBeVisible();
    await expect(
      profiler.getByRole("heading", { name: "Query plans", exact: true }),
    ).toBeVisible();
    await profiler
      .locator("summary")
      .filter({ hasText: "Cell cell-0" })
      .first()
      .click();
    await expect(
      profiler
        .locator("pre")
        .filter({ hasText: /Aggregate|Exchange/ })
        .first(),
    ).toBeVisible();
    await profiler
      .getByRole("combobox", { name: "Compare run profile" })
      .selectOption(previous.id);
    await expect(
      profiler.locator('[aria-label="Run comparison"]'),
    ).toBeVisible();
    await expect(
      profiler.locator('[aria-label="Run comparison"]'),
    ).toContainText("duration ms");
    await expect(
      profiler.getByText("Spark metrics are unavailable.", { exact: true }),
    ).toHaveCount(0);
  } finally {
    if (current?.session_id) {
      const stopped = await api.post(
        `/api/resources/sessions/${current.session_id}/stop`,
        { data: { project_id: "default", cluster_id: "local" } },
      );
      expect(stopped.ok()).toBeTruthy();
    }
  }
});
