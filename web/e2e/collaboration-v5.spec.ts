import { test, expect, createNotebook } from "./helpers";
import type { Page } from "@playwright/test";
import { readFile } from "node:fs/promises";

async function openNotebook(page: Page, name: string) {
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Notebooks", exact: true })
    .click();
  await page.getByLabel("Search notebooks in all folders").fill(name);
  await page.getByText(name, { exact: true }).click();
  await page
    .locator(".notebook-toolbar")
    .getByRole("button", { name: "Collaborate", exact: true })
    .click();
  return page.getByRole("dialog", { name: "Collaborate on notebook" });
}

test("two teammates coedit separate cells, discuss, review, and invalidate an approval after a source edit", async ({
  admin,
  api,
  browser,
  playwright,
  prefix,
}) => {
  const book = await createNotebook(api, `${prefix}-collaboration`, [
    "left = 1",
    "right = 2",
  ]);
  const password = `${prefix}-disposable-team-password`;
  const response = await api.post("/api/users", {
    data: { username: `${prefix}-peer`, password, role: "member" },
  });
  expect(response.ok()).toBeTruthy();
  const peer = await response.json();
  const peerApi = await playwright.request.newContext({
    baseURL: "http://127.0.0.1:3001",
  });
  expect(
    (
      await peerApi.post("/api/auth/login", {
        data: { username: peer.username, password },
      })
    ).ok(),
  ).toBeTruthy();
  const peerContext = await browser.newContext({
    baseURL: "http://127.0.0.1:3001",
    viewport: { width: 1440, height: 1000 },
    storageState: await peerApi.storageState(),
  });
  const page = await peerContext.newPage();
  try {
    const room = await openNotebook(admin, book.name);
    await room
      .getByRole("button", { name: "Enable live editing", exact: true })
      .click();
    await room
      .locator(".v5-live-cell")
      .nth(0)
      .getByRole("button", { name: "Edit cell", exact: true })
      .click();
    await room.getByLabel("Live cell 1", { exact: true }).fill("left = 100");
    await expect
      .poll(
        async () =>
          (await (await api.get(`/api/notebooks/${book.id}`)).json()).cells[0]
            .source,
      )
      .toBe("left = 100");
    await page.goto("/");
    const other = await openNotebook(page, book.name);
    await expect(
      other
        .locator(".v5-live-cell")
        .nth(0)
        .getByRole("button", { name: "Edit cell", exact: true }),
    ).toBeDisabled();
    await other
      .locator(".v5-live-cell")
      .nth(1)
      .getByRole("button", { name: "Edit cell", exact: true })
      .click();
    await other.getByLabel("Live cell 2", { exact: true }).fill("right = 200");
    await expect
      .poll(async () =>
        (await (await api.get(`/api/notebooks/${book.id}`)).json()).cells.map(
          (cell: { source: string }) => cell.source,
        ),
      )
      .toEqual(["left = 100", "right = 200"]);
    await other
      .getByRole("button", { name: "Finish editing", exact: true })
      .click();
    await admin.bringToFront();
    await room
      .getByRole("button", { name: "Finish editing", exact: true })
      .click();
    await room
      .getByLabel("Comment", { exact: true })
      .fill("Please check both saved cells");
    await room
      .getByRole("button", { name: "Post comment", exact: true })
      .click();
    await page.bringToFront();
    const thread = other
      .locator(".v5-comment")
      .filter({ hasText: "Please check both saved cells" });
    await thread.getByRole("button", { name: "Reply", exact: true }).click();
    await other
      .getByLabel("Comment", { exact: true })
      .fill("Both changes are visible");
    await other
      .getByRole("button", { name: "Post comment", exact: true })
      .click();
    await admin.bringToFront();
    await expect(
      room.getByText("Both changes are visible", { exact: true }),
    ).toBeVisible();
    await room.getByRole("button", { name: "Reviews", exact: true }).click();
    await room
      .getByLabel("Reviewer account ID", { exact: true })
      .selectOption(peer.id);
    await room
      .getByLabel("Review message", { exact: true })
      .fill("Review the saved version");
    await room
      .getByRole("button", { name: "Request review", exact: true })
      .click();
    await page.bringToFront();
    await other.getByRole("button", { name: "Reviews", exact: true }).click();
    await other.getByRole("button", { name: "Approve", exact: true }).click();
    await expect(other.locator(".status-approved")).toBeVisible();
    await admin.bringToFront();
    await expect(room.locator(".status-approved")).toBeVisible();
    await room
      .locator(".v5-live-cell")
      .nth(0)
      .getByRole("button", { name: "Edit cell", exact: true })
      .click();
    await room.getByLabel("Live cell 1", { exact: true }).fill("left = 300");
    await expect
      .poll(
        async () =>
          (await (await api.get(`/api/notebooks/${book.id}`)).json()).cells[0]
            .source,
      )
      .toBe("left = 300");
    await expect(room.locator(".status-stale")).toBeVisible();
    await room
      .getByRole("button", { name: "Finish editing", exact: true })
      .click();
    await room
      .getByRole("button", { name: "Disable live editing", exact: true })
      .click();
    await room
      .getByRole("button", { name: "Close dialog", exact: true })
      .click();
    await expect(
      admin.getByLabel("Notebook name", { exact: true }),
    ).toHaveValue(book.name);
    expect((await api.delete(`/api/notebooks/${book.id}`)).ok()).toBeTruthy();
  } finally {
    await peerContext.close();
    await peerApi.dispose();
  }
});

test("project owners can filter and export audit history through the UI", async ({
  admin,
  api,
  prefix,
}, testInfo) => {
  const book = await createNotebook(api, `${prefix}-audit`);
  const comment = await api.post(`/api/notebooks/${book.id}/comments`, {
    data: { text: `${prefix}-audit-evidence` },
  });
  expect(comment.ok()).toBeTruthy();
  const id = (await comment.json()).id;
  await admin
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Audit history", exact: true })
    .click();
  await admin.getByLabel("Action", { exact: true }).fill("comment.create");
  await expect(
    admin.getByRole("cell", { name: id, exact: true }),
  ).toBeVisible();
  const promise = admin.waitForEvent("download");
  await admin.getByRole("link", { name: "Export CSV", exact: true }).click();
  const download = await promise;
  const path = testInfo.outputPath("audit.csv");
  await download.saveAs(path);
  const text = await readFile(path, "utf8");
  expect(text).toContain("comment.create");
  expect(text).toContain(id);
  expect((await api.delete(`/api/notebooks/${book.id}`)).ok()).toBeTruthy();
});
