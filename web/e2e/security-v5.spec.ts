import { test, expect, createNotebook } from "./helpers";
import type { APIRequestContext, Page } from "@playwright/test";

async function openNotebook(page: Page, name: string) {
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Notebooks", exact: true })
    .click();
  await page.getByLabel("Search current view").fill(name);
  await page.getByText(name, { exact: true }).click();
  await expect(page.getByLabel("Notebook name")).toHaveValue(name);
}

async function runFromUI(
  page: Page,
  api: APIRequestContext,
  notebookId: string,
  cell?: number,
) {
  const responsePromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `/api/notebooks/${notebookId}/run` &&
      response.request().method() === "POST",
  );
  if (cell === undefined)
    await page.getByRole("button", { name: "Run all", exact: true }).click();
  else
    await page
      .getByRole("button", { name: "Run cell", exact: true })
      .nth(cell)
      .click();
  const response = await responsePromise;
  expect(response.status()).toBe(202);
  const submitted = await response.json();
  await expect
    .poll(
      async () =>
        (await (await api.get(`/api/runs/${submitted.id}`)).json()).status,
      { timeout: 90_000 },
    )
    .toBe("succeeded");
  return (await api.get(`/api/runs/${submitted.id}`)).json();
}

test("notebook secrets are created, selected, redacted and rotated through the real UI and compute", async ({
  admin,
  api,
  playwright,
  prefix,
}) => {
  const scopeName = `${prefix}-credentials`;
  const firstSecret = `${prefix}-first-fixture-credential`;
  const secondSecret = `${prefix}-rotated-fixture-credential`;
  const book = await createNotebook(api, `${prefix}-secret-notebook`, [
    `old_secret = dbutils.secrets.get(${JSON.stringify(scopeName)}, "token")\nassert notebook_secrets.get(${JSON.stringify(scopeName)}, "token") == old_secret\nprint("safe-prefix:" + old_secret[:8], end="", flush=True)\nprint(old_secret[8:])\ndisplay([{old_secret: old_secret}])`,
    `print("retained:", old_secret)\nprint("current:", dbutils.secrets.get(${JSON.stringify(scopeName)}, "token"))`,
  ]);
  await admin
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Notebook secrets", exact: true })
    .click();
  await admin.getByRole("button", { name: "New scope", exact: true }).click();
  let modal = admin.getByRole("dialog", { name: "New secret scope" });
  await modal.getByLabel("Scope name").fill(scopeName);
  await modal
    .getByLabel("Description")
    .fill("Disposable browser-test credentials");
  await modal.getByRole("button", { name: "Save scope", exact: true }).click();
  await expect(modal).not.toBeVisible();
  const card = admin
    .locator("article")
    .filter({
      has: admin.getByRole("heading", { name: scopeName, exact: true }),
    });
  await card.getByRole("button", { name: "Add key", exact: true }).click();
  modal = admin.getByRole("dialog", { name: `Add key to ${scopeName}` });
  await modal.getByLabel("Key name").fill("token");
  await modal.getByLabel("Secret value").fill(firstSecret);
  await modal.getByRole("button", { name: "Save secret", exact: true }).click();
  await expect(modal).not.toBeVisible();
  await expect(card.getByText("Version 1", { exact: false })).toBeVisible();
  const scopes = await (
    await api.get("/api/secret-scopes?project_id=default&limit=100")
  ).json();
  expect(JSON.stringify(scopes)).not.toContain(firstSecret);
  const scope = scopes.items.find(
    (item: { name: string }) => item.name === scopeName,
  );
  expect(scope).toBeTruthy();

  await openNotebook(admin, book.name);
  await admin
    .locator(".notebook-toolbar")
    .getByRole("button", { name: "Notebook secrets", exact: true })
    .click();
  modal = admin.getByRole("dialog", { name: "Notebook secrets", exact: true });
  await modal
    .getByLabel("Secret scope", { exact: true })
    .selectOption(scopeName);
  await modal.getByRole("checkbox", { name: "token", exact: true }).check();
  await modal
    .getByRole("button", { name: "Save secret selection", exact: true })
    .click();
  await expect(modal.getByRole("status")).toContainText("selection saved");
  await modal
    .getByRole("button", { name: "Close dialog", exact: true })
    .click();
  const firstRun = await runFromUI(admin, api, book.id);
  expect(JSON.stringify(firstRun)).not.toContain(firstSecret);
  expect(JSON.stringify(firstRun.outputs)).toContain("[REDACTED]");
  await expect(
    admin.getByText("safe-prefix:[REDACTED]", { exact: false }).first(),
  ).toBeVisible();

  await admin
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Notebook secrets", exact: true })
    .click();
  await card.getByRole("button", { name: "Rotate", exact: true }).click();
  modal = admin.getByRole("dialog", { name: `Rotate ${scopeName}/token` });
  await expect(modal.getByLabel("Secret value")).toHaveValue("");
  await modal.getByLabel("Secret value").fill(secondSecret);
  await modal.getByRole("button", { name: "Save secret", exact: true }).click();
  await expect(modal).not.toBeVisible();
  await expect(card.getByText("Version 2", { exact: false })).toBeVisible();
  await openNotebook(admin, book.name);
  const rotatedRun = await runFromUI(admin, api, book.id, 1);
  expect(JSON.stringify(rotatedRun)).not.toContain(firstSecret);
  expect(JSON.stringify(rotatedRun)).not.toContain(secondSecret);
  expect(rotatedRun.outputs[0].stdout).toContain("retained: [REDACTED]");
  expect(rotatedRun.outputs[0].stdout).toContain("current: [REDACTED]");

  const viewerName = `${prefix}-viewer`;
  const viewerPassword = `${prefix}-disposable-viewer-password`;
  const viewerResponse = await api.post("/api/users", {
    data: { username: viewerName, password: viewerPassword, role: "member" },
  });
  expect(viewerResponse.ok()).toBeTruthy();
  const viewer = await viewerResponse.json();
  expect(
    (
      await api.put(`/api/projects/default/members/${viewer.id}`, {
        data: { role: "viewer" },
      })
    ).ok(),
  ).toBeTruthy();
  const context = await playwright.request.newContext({
    baseURL: "http://127.0.0.1:3001",
  });
  try {
    expect(
      (
        await context.post("/api/auth/login", {
          data: { username: viewerName, password: viewerPassword },
        })
      ).ok(),
    ).toBeTruthy();
    const metadata = await context.get(
      "/api/secret-scopes?project_id=default&limit=100",
    );
    expect(metadata.ok()).toBeTruthy();
    expect(await metadata.text()).not.toContain(secondSecret);
    expect(
      (
        await context.put(`/api/secret-scopes/${scope.id}/keys/token`, {
          data: { value: "rejected-value" },
        })
      ).status(),
    ).toBe(403);
    expect(
      (
        await context.put(`/api/notebooks/${book.id}/secret-refs`, {
          data: { secret_refs: [] },
        })
      ).status(),
    ).toBe(403);
    expect(
      (await context.get(`/api/secret-scopes/${scope.id}/keys/token`)).status(),
    ).toBe(405);
  } finally {
    await context.dispose();
  }
  expect((await api.delete(`/api/notebooks/${book.id}`)).ok()).toBeTruthy();
  expect(
    (await api.delete(`/api/secret-scopes/${scope.id}`)).ok(),
  ).toBeTruthy();
});

test("browser OIDC login uses the actual provider and callback while local administrator login stays available", async ({
  browser,
}) => {
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    await page.goto("http://127.0.0.1:3001/");
    await page.getByRole("link", { name: /^Continue with / }).click();
    await expect(
      page.getByRole("heading", { name: "OpenBricks test sign-in" }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Continue as Alice", exact: true })
      .click();
    await expect(
      page.getByRole("navigation", { name: "Main navigation" }),
    ).toBeVisible();
    const current = await (
      await page.request.get("http://127.0.0.1:3001/api/auth/me")
    ).json();
    expect(current.auth_provider).toBe("oidc");
    expect(current.role).toBe("member");
    expect(current.display_name).toBe("Smoke Alice");
    const projects = await (
      await page.request.get("http://127.0.0.1:3001/api/projects")
    ).json();
    expect(
      projects.find((item: { id: string }) => item.id === "default").role,
    ).toBe("editor");
    await page
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("button", { name: "Notebook secrets", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "New scope", exact: true }),
    ).toBeDisabled();
  } finally {
    await context.close();
  }
  const local = await browser.newContext();
  try {
    const page = await local.newPage();
    await page.goto("http://127.0.0.1:3001/");
    await page.getByLabel("Username", { exact: true }).fill("admin");
    await page
      .getByLabel("Password", { exact: true })
      .fill(process.env.OPENBRICKS_SMOKE_PASSWORD!);
    await page
      .getByRole("button", { name: "Sign in to workspace", exact: true })
      .click();
    await expect(
      page.getByRole("navigation", { name: "Main navigation" }),
    ).toBeVisible();
    expect(
      (
        await (
          await page.request.get("http://127.0.0.1:3001/api/auth/me")
        ).json()
      ).role,
    ).toBe("admin");
  } finally {
    await local.close();
  }
});
