import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// 开发时 /api 与 /ws 请求代理到本机后端，前端代码只使用相对路径
const backend = process.env.SA_BACKEND_URL ?? 'http://127.0.0.1:18080'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    host: '127.0.0.1',
    port: 15173,
    strictPort: true,
    // 使用手册直接读取仓库根目录下的 docs/USER_GUIDE.md
    fs: { allow: ['.', '../docs'] },
    proxy: {
      '/api': { target: backend, changeOrigin: true },
      '/ws': { target: backend.replace(/^http/, 'ws'), ws: true },
    },
  },
  build: {
    // 本地应用，Element Plus 全量引入换取开发便利，主包约 1.2MB 可接受
    chunkSizeWarningLimit: 1600,
  },
  test: {
    environment: 'jsdom',
  },
})
