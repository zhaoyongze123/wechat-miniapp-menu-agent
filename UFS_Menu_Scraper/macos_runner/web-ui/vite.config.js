import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 产物固定为 task-center.js / task-center.css：Windows 安装包与 Hub 镜像只拷贝 web/，
// 运行时既没有 npm 也没有开发服务器，因此这里锁定库模式与固定文件名。
export default defineConfig({
  plugins: [vue()],
  base: '/vue/',
  // 库模式不会自动替换 Node 环境变量，浏览器里没有 process，必须显式定义。
  define: {
    'process.env.NODE_ENV': JSON.stringify('production'),
    'process.env': '{}',
    __VUE_OPTIONS_API__: 'true',
    __VUE_PROD_DEVTOOLS__: 'false',
    __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false',
  },
  build: {
    outDir: '../web/vue',
    emptyOutDir: true,
    cssCodeSplit: false,
    lib: {
      entry: 'src/main.js',
      name: 'MenuTaskCenter',
      formats: ['iife'],
      fileName: () => 'task-center.js',
    },
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
        assetFileNames: (asset) => {
          const names = [...(asset.names || []), asset.name || '']
          return names.some((name) => name.endsWith('.css')) ? 'task-center.css' : '[name][extname]'
        },
      },
    },
  },
})
