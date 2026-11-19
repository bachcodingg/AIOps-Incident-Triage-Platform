import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` serves the console on :5173 and sends /api to a running
// `make console` (aiops/loop, :8090). `npm run build` writes dist/, which
// `make console` serves itself.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8090" } },
  build: { outDir: "dist", emptyOutDir: true },
});
