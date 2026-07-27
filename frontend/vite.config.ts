import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Tenantless web console build config.
//
// base '/ui/'  — assets emit as /ui/assets/* so the built dist is byte-compatible with the
//                embedded-prod mount (the axum `/ui` nest, D-06 / WEBUI-03, 15-02).
// server.proxy — DEV same-origin story (D-05 / WEBUI-02): the Vite dev server forwards the ARM +
//                overlay API prefixes to the running axum server on :8080. Because every fetch()
//                uses an ABSOLUTE api path (/_sim/*, /subscriptions/*, ...) — never under /ui — the
//                browser talks to a single origin (localhost:5173) and axum needs NO CORS.
//                Do NOT add CORS to axum for the dev origin.
export default defineConfig({
  base: '/ui/',
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/subscriptions': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/_sim': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/token': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/_console': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      // For `serve --tls` (https :8443): target 'https://127.0.0.1:8443', secure: false.
    },
  },
});
