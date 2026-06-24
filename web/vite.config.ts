import { sveltekit } from "@sveltejs/kit/vite";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [sveltekit()],
  // Dev only: proxy API + MCP to the running backend so `vite dev` works
  // against http://localhost:33000 without CORS. Production is same-origin.
  server: {
    proxy: {
      "/api": "http://localhost:33000",
      "/mcp": "http://localhost:33000",
    },
  },
});
