import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './e2e', fullyParallel: false, workers: 1, timeout: 45000,
  expect: { timeout: 12000 }, reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:18765', headless: true, viewport: { width: 1440, height: 1100 },
    launchOptions: { args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', '--autoplay-policy=no-user-gesture-required'] },
    screenshot: 'only-on-failure', trace: 'off',
  },
  webServer: { command: 'uv run --locked python frontend/e2e/server.py', cwd: '..', url: 'http://127.0.0.1:18765/health/live', reuseExistingServer: false, timeout: 120000 },
});
