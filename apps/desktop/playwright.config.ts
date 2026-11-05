import { defineConfig } from '@playwright/test'

// End-to-end tests launch the built app (out/) through Playwright's Electron support.
// `pnpm test:e2e` builds first.
export default defineConfig({
  testDir: 'test/e2e',
  timeout: 30_000,
  workers: 1,
  reporter: 'list',
  use: { screenshot: 'only-on-failure', trace: 'retain-on-failure' }
})
