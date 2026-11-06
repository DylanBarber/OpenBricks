import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

const baseURL = (
  process.env.OPENBRICKS_SMOKE_URL || "http://127.0.0.1:3001"
).replace(/\/$/, "");
const repository = path.resolve(__dirname, "..");
if (baseURL.replace(/\/$/, "") !== "http://127.0.0.1:3001")
  throw new Error(
    "Browser E2E tests may use only the disposable OpenBricks smoke server on port 3001",
  );
if (!process.env.OPENBRICKS_SMOKE_PASSWORD)
  throw new Error(
    "Set the disposable smoke administrator password; production .env is never loaded",
  );

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  outputDir: path.join(repository, ".test-data/e2e/artifacts"),
  reporter: [
    ["list"],
    [
      "json",
      { outputFile: path.join(repository, ".test-data/e2e/results.json") },
    ],
  ],
  use: {
    baseURL,
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    actionTimeout: 15_000,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
