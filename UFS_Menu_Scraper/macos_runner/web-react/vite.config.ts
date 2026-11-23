import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 产物直接装配到 Python 服务的 web/react/：资源基址固定 /react/assets/，
// 由 FastAPI 的 /react/assets/{path} 路由按清单白名单返回，不依赖 Node 运行。
export default defineConfig({
  base: '/react/',
  plugins: [react(), tailwindcss()],
  build: {
    outDir: '../web/react',
    emptyOutDir: true,
    assetsDir: 'assets',
    manifest: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8765',
    },
  },
})
