import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The generated ACI contract types live outside apps/ui in the workspace package.
// Alias @aci/* so fixtures can type trace/session data against the frozen contract.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@aci": fileURLToPath(
        new URL("../../packages/aci-protocol/generated/ts", import.meta.url),
      ),
    },
  },
  server: { port: 5173 },
  preview: { port: 4173 },
});
