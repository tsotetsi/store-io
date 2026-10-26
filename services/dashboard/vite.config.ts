import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In production nginx serves the build and proxies /api to control-service
// (see nginx.conf). This proxy gives `npm run dev` the same behaviour locally.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        // `make dashboard-dev` publishes the control service on this port.
        target: "http://localhost:8010",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
