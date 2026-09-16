import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  build: {
    emptyOutDir: true,
    target: "es2021",
  },
  clearScreen: false,
  plugins: [react()],
  server: {
    port: 1420,
    strictPort: true,
  },
});
