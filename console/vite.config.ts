import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// The build is committed into the Python package, so services never need Node:
// packages/agentkit-channels/src/agentkit/channels/static/console
const backend = process.env.AGENTKIT_CONSOLE_BACKEND || "http://127.0.0.1:8000";

export default defineConfig({
  base: "/console/",
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: {
    outDir: "../packages/agentkit-channels/src/agentkit/channels/static/console",
    emptyOutDir: true,
    assetsInlineLimit: 0, // CSP: no data: fonts or scripts
    sourcemap: false,
    chunkSizeWarningLimit: 600,
    // Lazy pages are imported relative to the entry chunk; the preload helper would use absolute /console/
    // URLs, which break when the console is mounted under a prefix (the server rewrites only HTML and CSS).
    modulePreload: false,
  },
  server: {
    proxy: { "/v1": backend, "/readyz": backend, "/healthz": backend },
  },
});
