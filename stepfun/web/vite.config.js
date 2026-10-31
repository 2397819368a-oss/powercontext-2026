import { defineConfig } from "vite";

// transformers.js 自己会起 Worker 并按需拉 wasm/onnx，
// 交给 Vite 做依赖预打包会把它内部的 worker 引用改写坏，必须排除。
export default defineConfig({
  optimizeDeps: {
    exclude: ["@xenova/transformers"],
  },
  worker: {
    format: "es",
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
  },
  build: {
    target: "es2022",
  },
});
