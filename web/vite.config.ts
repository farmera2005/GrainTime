import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the api runs on :8000; in the stack nginx proxies /api.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
});
