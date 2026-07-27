import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Vitest (Wave-0 test infra). jsdom env + globals so component tests (later plans) get a DOM;
// the react plugin is loaded so `.tsx` suites transform. NEVER watch mode — agent/CI runs are
// single-shot (`npm run test` == `vitest run`).
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
