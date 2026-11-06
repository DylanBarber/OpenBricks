import { createHash } from "node:crypto";
import type { APIRequestContext, Page } from "@playwright/test";
import { test, expect, createNotebook } from "./helpers";

async function openSection(page: Page, name: string) {
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name, exact: true })
    .click();
}

async function refreshFromUI(
  page: Page,
  api: APIRequestContext,
  dashboardId: string,
  expected = "succeeded",
) {
  const response = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `/api/dashboards/${dashboardId}/refresh` &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Refresh data", exact: true }).click();
  const submitted = await response;
  expect(submitted.status()).toBe(202);
  const refresh = await submitted.json();
  await expect
    .poll(
      async () =>
        (
          await (
            await api.get(
              `/api/dashboards/${dashboardId}/refreshes/${refresh.id}`,
            )
          ).json()
        ).status,
      { timeout: 90_000, intervals: [500, 1000] },
    )
    .toBe(expected);
  return refresh;
}

test("saved-query dashboards persist layouts and filters, preserve failed-refresh caches, and stay read-only for viewers", async ({
  admin,
  api,
  browser,
  playwright,
  prefix,
}) => {
  test.setTimeout(240_000);
  const queryResponse = await api.post("/api/data/queries", {
    data: {
      project_id: "default",
      name: `${prefix}-total-query`,
      engine: "duckdb",
      sql: "SELECT ? AS total",
      params: [7],
    },
  });
  expect(queryResponse.ok()).toBeTruthy();
  const saved = await queryResponse.json();
  let dashboardId: string | undefined;
  try {
    await openSection(admin, "Dashboards");
    await admin
      .getByRole("button", { name: "New dashboard", exact: true })
      .click();
    const dialog = admin.getByRole("dialog", {
      name: "Create dashboard",
      exact: true,
    });
    await dialog
      .getByLabel("Name", { exact: true })
      .fill(`${prefix}-dashboard`);
    await dialog
      .getByLabel("Description", { exact: true })
      .fill("A dashboard created by the full browser smoke test");
    await dialog
      .getByRole("button", { name: "Add parameter", exact: true })
      .click();
    const parameter = dialog.locator(".parameter-row");
    await parameter.getByLabel("Name", { exact: true }).fill("minimum");
    await parameter.getByLabel("Type", { exact: true }).selectOption("integer");
    await parameter.getByLabel("Default", { exact: true }).fill("7");
    const tile = dialog.locator("fieldset.v5-tile-editor").first();
    await tile.getByLabel("Title", { exact: true }).fill("Order total");
    await tile
      .getByLabel("Visualization", { exact: true })
      .selectOption("number");
    await tile.getByLabel("Find saved query", { exact: true }).fill(saved.name);
    await expect(tile.getByLabel("Saved query", { exact: true })).toContainText(
      saved.name,
    );
    await tile
      .getByLabel("Saved query", { exact: true })
      .selectOption(saved.id);
    await tile
      .getByRole("button", { name: "Bind a filter to this query", exact: true })
      .click();
    await tile
      .getByLabel("Dashboard filter", { exact: true })
      .selectOption("minimum");
    await tile.getByLabel("Width", { exact: true }).selectOption("6");
    const createdResponse = admin.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/dashboards" &&
        response.request().method() === "POST",
    );
    await dialog
      .getByRole("button", { name: "Save dashboard", exact: true })
      .click();
    const created = await createdResponse;
    expect(created.status()).toBe(201);
    dashboardId = (await created.json()).id;
    await expect(dialog).not.toBeVisible();
    await expect(
      admin.getByRole("heading", { name: `${prefix}-dashboard`, exact: true }),
    ).toBeVisible();
    await refreshFromUI(admin, api, dashboardId!);
    await expect(admin.locator(".v5-number-result")).toHaveText("7");
    await expect(admin.locator(".v5-cache-stamp")).toContainText("fresh");
    const persisted = await (
      await api.get(`/api/dashboards/${dashboardId}`)
    ).json();
    expect(persisted.tiles[0].width).toBe(6);
    expect(persisted.tiles[0].parameter_bindings).toEqual({ "0": "minimum" });

    await admin
      .locator(".v5-dashboard-filters")
      .getByRole("textbox")
      .fill("42");
    await admin
      .getByRole("button", { name: "View cached results", exact: true })
      .click();
    await expect(
      admin.getByText("There is no saved result for these filters yet.", {
        exact: false,
      }),
    ).toBeVisible();
    await refreshFromUI(admin, api, dashboardId!);
    await expect(admin.locator(".v5-number-result")).toHaveText("42");
    const originalSnapshot = await (
      await api.get(
        `/api/dashboards/${dashboardId}/snapshot?parameters=${encodeURIComponent(JSON.stringify({ minimum: 42 }))}`,
      )
    ).json();

    const broken = await api.put(`/api/data/queries/${saved.id}`, {
      data: {
        ...saved,
        sql: `SELECT * FROM missing_${prefix.replaceAll("-", "_")}`,
      },
    });
    expect(broken.ok()).toBeTruthy();
    await refreshFromUI(admin, api, dashboardId!, "failed");
    await expect(admin.locator(".v5-number-result")).toHaveText("42");
    await expect(admin.locator(".v5-cache-stamp")).toContainText("stale");
    const preserved = await (
      await api.get(
        `/api/dashboards/${dashboardId}/snapshot?parameters=${encodeURIComponent(JSON.stringify({ minimum: 42 }))}`,
      )
    ).json();
    expect(preserved.refresh_id).toBe(originalSnapshot.refresh_id);
    expect(preserved.latest_refresh.status).toBe("failed");

    const username = `${prefix}-dashboard-viewer`,
      password = `${prefix}-disposable-viewer-password`;
    const createdViewer = await api.post("/api/users", {
      data: { username, password, role: "member" },
    });
    expect(createdViewer.ok()).toBeTruthy();
    const viewer = await createdViewer.json();
    expect(
      (
        await api.put(`/api/projects/default/members/${viewer.id}`, {
          data: { role: "viewer" },
        })
      ).ok(),
    ).toBeTruthy();
    const viewerAPI = await playwright.request.newContext({
      baseURL: "http://127.0.0.1:3001",
    });
    try {
      expect(
        (
          await viewerAPI.post("/api/auth/login", {
            data: { username, password },
          })
        ).ok(),
      ).toBeTruthy();
      const context = await browser.newContext({
        storageState: await viewerAPI.storageState(),
      });
      try {
        const page = await context.newPage();
        await page.goto("/");
        await openSection(page, "Dashboards");
        await page
          .getByLabel("Find dashboard", { exact: true })
          .fill(`${prefix}-dashboard`);
        await page
          .locator(".v5-dashboard-choice")
          .filter({ hasText: `${prefix}-dashboard` })
          .click();
        await expect(
          page.getByRole("heading", {
            name: `${prefix}-dashboard`,
            exact: true,
          }),
        ).toBeVisible();
        const before = (
          await (
            await api.get(
              `/api/data/executions?project_id=default&query_id=${saved.id}`,
            )
          ).json()
        ).total;
        await expect(
          page.getByRole("button", { name: "New dashboard", exact: true }),
        ).toBeDisabled();
        await expect(
          page.getByRole("button", { name: "Refresh data", exact: true }),
        ).toBeDisabled();
        await expect(
          page.getByRole("button", { name: "Edit dashboard", exact: true }),
        ).toHaveCount(0);
        await page
          .locator(".v5-dashboard-filters")
          .getByRole("textbox")
          .fill("42");
        await page
          .getByRole("button", { name: "View cached results", exact: true })
          .click();
        await expect(page.locator(".v5-number-result")).toHaveText("42");
        const after = (
          await (
            await api.get(
              `/api/data/executions?project_id=default&query_id=${saved.id}`,
            )
          ).json()
        ).total;
        expect(after).toBe(before);
        expect(
          (
            await viewerAPI.post(`/api/dashboards/${dashboardId}/refresh`, {
              data: {},
            })
          ).status(),
        ).toBe(403);
      } finally {
        await context.close();
      }
    } finally {
      await viewerAPI.dispose();
    }
  } finally {
    if (dashboardId)
      expect(
        (await api.delete(`/api/dashboards/${dashboardId}`)).ok(),
      ).toBeTruthy();
    expect(
      (await api.delete(`/api/data/queries/${saved.id}`)).ok(),
    ).toBeTruthy();
  }
});

test("real dataset writes build the lineage graph and quality history; owner editing and asset pagination work in the browser", async ({
  admin,
  api,
  prefix,
}) => {
  test.setTimeout(240_000);
  const source = `blob://workspace/${prefix}/source`,
    target = `blob://workspace/${prefix}/target`;
  const sourceCode = `frame = spark.createDataFrame([(1, 10), (2, 20)], ['OrderId', 'Amount'])\ndata.write(frame, {'path': ${JSON.stringify(source)}, 'format': 'parquet'}, mode='overwrite')\norders = data.read({'path': ${JSON.stringify(source)}, 'format': 'parquet'})\ndata.write(orders, {'path': ${JSON.stringify(target)}, 'format': 'delta'}, mode='overwrite')\ndata.quality(orders, [{'type': 'not_null', 'column': 'OrderId'}, {'type': 'row_count', 'min': 1}], dataset=${JSON.stringify(target)})`;
  const notebook = await createNotebook(api, `${prefix}-asset-evidence`, [
    sourceCode,
  ]);
  try {
    const response = await api.post(`/api/notebooks/${notebook.id}/run`, {
      data: { resource_options: { mode: "job", memory_mb: 1536 } },
    });
    expect(response.ok()).toBeTruthy();
    const submitted = await response.json();
    await expect
      .poll(
        async () =>
          (await (await api.get(`/api/runs/${submitted.id}`)).json()).status,
        { timeout: 180_000, intervals: [500, 1000] },
      )
      .toBe("succeeded");
    await openSection(admin, "Data assets & lineage");
    await admin.getByLabel("Find data asset", { exact: true }).fill(prefix);
    await admin.locator(".v5-asset-choice").filter({ hasText: target }).click();
    await expect(
      admin.getByRole("group", {
        name: "Observed dataset dependency graph",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      admin.getByRole("button", {
        name: `Focus dataset ${source}`,
        exact: true,
      }),
    ).toBeVisible();
    await admin
      .getByRole("button", { name: `Focus dataset ${source}`, exact: true })
      .click();
    await expect(admin.locator(".v5-focus-label")).toContainText(source);
    await admin.locator(".v5-asset-choice").filter({ hasText: target }).click();
    await expect(
      admin
        .locator(".v5-asset-detail")
        .getByRole("heading", { name: target, exact: true }),
    ).toBeVisible();
    await admin
      .getByRole("button", { name: "Edit asset details", exact: true })
      .click();
    const dialog = admin.getByRole("dialog", {
      name: "Edit data asset",
      exact: true,
    });
    await dialog
      .getByLabel("Description", { exact: true })
      .fill(`${prefix} documented reporting dataset`);
    await dialog
      .getByLabel("Owner", { exact: true })
      .selectOption({ label: "admin" });
    await dialog
      .getByLabel("Freshness threshold (minutes)", { exact: false })
      .fill("60");
    await dialog
      .getByRole("button", { name: "Save asset details", exact: true })
      .click();
    await expect(dialog).not.toBeVisible();
    await expect(admin.locator(".v5-asset-detail")).toContainText(
      `${prefix} documented reporting dataset`,
    );
    await expect(admin.locator(".v5-asset-facts")).toContainText("admin");
    await admin
      .getByRole("button", { name: "View quality history", exact: true })
      .click();
    const history = admin.getByRole("dialog", {
      name: "Observed quality history",
      exact: true,
    });
    await history
      .getByRole("button", { name: "Inspect checks", exact: true })
      .first()
      .click();
    await expect(history.locator(".v5-quality-checks")).toContainText(
      "not_null:OrderId",
    );
    await expect(history.locator(".v5-quality-checks")).toContainText("Passed");
    await history
      .getByRole("button", { name: "Close dialog", exact: true })
      .click();

    for (let index = 0; index < 21; index++) {
      const dataset = `catalog.${prefix.replaceAll("-", "_")}_page_${String(index).padStart(2, "0")}`;
      const id = createHash("sha256")
        .update(JSON.stringify(["default", dataset]))
        .digest("hex");
      expect(
        (
          await api.put(`/api/assets/${id}`, {
            data: {
              project_id: "default",
              dataset,
              description: "Pagination fixture",
            },
          })
        ).ok(),
      ).toBeTruthy();
    }
    await admin
      .getByLabel("Find data asset", { exact: true })
      .fill(`${prefix.replaceAll("-", "_")}_page_`);
    const assets = admin.locator(".v5-assets-list");
    await expect(assets.locator(".v5-asset-choice")).toHaveCount(20);
    await assets.getByRole("button", { name: "Next", exact: true }).click();
    await expect(assets.locator(".v5-asset-choice")).toHaveCount(1);
    expect(
      (await api.get("/api/assets/graph?project_id=default&depth=3")).status(),
    ).toBe(422);
    expect(
      (await api.get("/api/assets?project_id=default&limit=101")).status(),
    ).toBe(422);
  } finally {
    expect(
      (await api.delete(`/api/notebooks/${notebook.id}`)).ok(),
    ).toBeTruthy();
  }
});
