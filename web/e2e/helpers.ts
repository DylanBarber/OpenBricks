import {
  test as base,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import { randomUUID } from "node:crypto";

export const test = base.extend<{
  admin: Page;
  api: APIRequestContext;
  prefix: string;
}>({
  prefix: async ({}, use) => {
    await use(`e2e-${randomUUID().slice(0, 8)}`);
  },
  api: async ({ playwright, baseURL }, use) => {
    if (baseURL !== "http://127.0.0.1:3001")
      throw new Error("Refusing a non-disposable API target");
    const api = await playwright.request.newContext({ baseURL });
    const login = await api.post("/api/auth/login", {
      data: {
        username: "admin",
        password: process.env.OPENBRICKS_SMOKE_PASSWORD,
      },
    });
    expect(login.ok()).toBeTruthy();
    try {
      await use(api);
    } finally {
      await api.dispose();
    }
  },
  admin: async ({ browser, api, baseURL }, use) => {
    const context = await browser.newContext({
      baseURL,
      viewport: { width: 1440, height: 1000 },
      storageState: await api.storageState(),
    });
    const page = await context.newPage();
    await page.goto("/");
    await expect(
      page.getByRole("navigation", { name: "Main navigation" }),
    ).toBeVisible();
    try {
      await use(page);
    } finally {
      await context.close();
    }
  },
});
export { expect };

export async function createNotebook(
  api: APIRequestContext,
  name: string,
  sources = ["print('hello')"],
) {
  const response = await api.post("/api/notebooks", {
    data: {
      name,
      project_id: "default",
      cells: sources.map((source, index) => ({
        id: `cell-${index}`,
        language: "python",
        source,
      })),
    },
  });
  expect(response.ok()).toBeTruthy();
  return response.json();
}
