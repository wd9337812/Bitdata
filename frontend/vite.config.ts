import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: path.resolve(__dirname, "../app/static"),
    emptyOutDir: true,
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: "charts", test: /node_modules[\\/]recharts|node_modules[\\/]d3-/ },
            { name: "react", test: /node_modules[\\/](react|react-dom)[\\/]/ },
          ],
        },
      },
    },
  },
});
