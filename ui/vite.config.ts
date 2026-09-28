import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    // Plain HTTP on purpose: this targets a locally-run `uvicorn` dev
    // instance (e.g. `uvicorn app.main:app --reload`) started outside
    // Docker, which has no TLS of its own. This is unrelated to the
    // Docker Compose stack, which serves HTTPS by default for both the
    // built UI image and the operations-manager container - see the
    // "HTTPS / TLS" section in the root README.
    proxy: {
      "/v1": "http://localhost:8090",
      "/api": "http://localhost:8090",
    },
  },
});
