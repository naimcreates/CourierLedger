import { defineConfig, devices } from '@playwright/test'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// CourierLedger M1 — Playwright end-to-end tests (Milestone 9, part 3).
// Against the REAL backend + REAL PostgreSQL. No API mocks anywhere.
// Synthetic fixture data only (backend/tests/data/generic_statement_fixture.csv).
//
// STATUS: RUN — Playwright 1.64.1 + Chromium headless shell installed
// 2026-11-01; the happy-path suite passes (see e2e/README.md).
//
// The backend needs its environment from m1/backend/.env (DATABASE_URL
// against courierledger_app as the runtime role, SECRET_KEY,
// SESSION_SECRET). That file is gitignored and mode 600; it is read here
// and passed only to the backend server process — never printed.

// ESM-safe __dirname (the frontend package is "type": "module").
const __dirname = path.dirname(fileURLToPath(import.meta.url))

function loadBackendEnv(): Record<string, string> {
  const env: Record<string, string> = {}
  const envPath = path.resolve(__dirname, '..', 'backend', '.env')
  for (const line of readFileSync(envPath, 'utf8').split('\n')) {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith('#') || !trimmed.includes('=')) continue
    const [key, ...rest] = trimmed.split('=')
    env[key.trim()] = rest.join('=').trim()
  }
  return env
}

const backendEnv = loadBackendEnv()

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: [
    {
      // FastAPI backend (real PostgreSQL via backend/.env).
      command: '.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000',
      cwd: path.resolve(__dirname, '..', 'backend'),
      url: 'http://127.0.0.1:8000/api/health',
      reuseExistingServer: true,
      timeout: 120_000,
      env: backendEnv,
    },
    {
      // Vite dev server (proxies /api to the backend on 127.0.0.1:8000).
      command: 'npm run dev -- --port 5173 --strictPort',
      url: 'http://localhost:5173',
      reuseExistingServer: true,
      timeout: 120_000,
    },
  ],
})
