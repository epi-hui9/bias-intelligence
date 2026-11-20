import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";

/* One self-contained HTML file for review: npm run build:single */
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  build: { outDir: "dist-single", emptyOutDir: true },
});
