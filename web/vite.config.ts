import { resolve } from "node:path";

import { defineConfig } from "vite";

export default defineConfig({
  build: {
    outDir: "../src/agentic_osdu/web",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        inventory: resolve(import.meta.dirname, "index.html"),
        manifest: resolve(import.meta.dirname, "manifest.html"),
      },
    },
  },
  server: {
    host: "127.0.0.1",
    fs: { strict: true },
  },
});
