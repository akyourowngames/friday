const path = require("path");

module.exports = {
  root: path.join(__dirname, "src", "renderer"),
  publicDir: path.join(__dirname, "public"),
  base: "./",
  esbuild: {
    jsx: "automatic"
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    // Electron always loads port 5173. Failing loudly is safer than silently
    // opening an older Vite process on 5173 while this one moves to 5175.
    strictPort: true
  },
  build: {
    outDir: path.join(__dirname, "dist"),
    emptyOutDir: true
  },
  resolve: {
    alias: {
      "@": path.join(__dirname, "src", "renderer")
    }
  }
};
