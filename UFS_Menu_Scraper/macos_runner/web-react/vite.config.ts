import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

/**
 * @openai/apps-sdk-ui 的样式入口包含 KaTeX 字体，字体文件指向外部 CDN。
 * 本项目不渲染数学公式，发布包又要求完全自包含，因此在构建末尾移除
 * 引用远程字体的 @font-face，避免离线环境或 CSP 下产生远程请求。
 */
function stripRemoteFontFaces(): Plugin {
  return {
    name: 'strip-remote-font-faces',
    apply: 'build',
    enforce: 'post',
    // KaTeX 样式由 CSS 插件在打包内部内联，只能在这里清理最终产物；
    // 产物进入版本发布后才启用不可变缓存，因此不影响后续升级。
    generateBundle(_options, bundle) {
      const pattern = /@font-face\{[^}]*https?:\/\/[^}]*\}/g
      for (const file of Object.values(bundle)) {
        if (file.type !== 'asset' || !file.fileName.endsWith('.css')) continue
        const source = typeof file.source === 'string' ? file.source : new TextDecoder().decode(file.source)
        const cleaned = source.replace(pattern, '')
        if (cleaned !== source) file.source = cleaned
      }
    },
  }
}

// 产物直接装配到 Python 服务的 web/react/：资源基址固定 /assets/，
// 由 FastAPI 的 /assets/{name} 路由按清单白名单返回，不依赖 Node 运行。
export default defineConfig({
  base: '/',
  plugins: [react(), tailwindcss(), stripRemoteFontFaces()],
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
