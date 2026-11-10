import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    vue()
  ],
  server: {
    proxy: {
      '/api': {
        // 默认指向 Python 版后端（正式实现，:8081）。
        // 需要切到遗留 Java 版时：start-local.sh --java 会注入 VITE_API_TARGET。
        // 业务代码（ChatWindow.vue）不需要任何改动。
        //
        // 注意用 127.0.0.1 而不是 localhost：Node 会把 localhost 优先解析成 IPv6 ::1，
        // 而 uvicorn 默认只监听 IPv4 127.0.0.1，结果是 ECONNREFUSED ::1:8081。
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8081',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''), // 去掉 /api 前缀
      },
    },
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
})
