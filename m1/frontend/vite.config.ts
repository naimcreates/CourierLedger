import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  test: {
    // Vitest must only collect the unit tests under src/. The Playwright
    // end-to-end suite lives in e2e/ and is run with `npx playwright
    // test` — its test.describe() is not a Vitest test and fails
    // collection ("Playwright Test did not expect test.describe()").
    include: ['src/**/*.test.{ts,tsx}'],
    environment: 'jsdom',
  },
  server: {
    // Dev proxy: the FastAPI backend is expected on port 8000.
    // With VITE_API_URL unset, the frontend calls same-origin /api/* and
    // Vite forwards those requests to the backend.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
