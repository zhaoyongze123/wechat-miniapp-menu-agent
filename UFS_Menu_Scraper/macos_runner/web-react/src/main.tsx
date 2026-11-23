import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import { HashRouter } from 'react-router-dom'
import './styles/index.css'
import { App } from './app/App'
import { queryClient } from './api/query'

const container = document.getElementById('root')
if (!container) throw new Error('缺少 React 挂载节点')

// 兼容计划中的 `/?ui=react#task-center` 写法：HashRouter 需要 #/path，
// 在首次渲染前把旧式 hash 归一化，地址栏只改一次、不新增历史记录。
if (window.location.hash.length > 1 && !window.location.hash.startsWith('#/')) {
  const normalized = `#/${window.location.hash.slice(1)}`
  window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}${normalized}`)
}

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <HashRouter>
        <App />
      </HashRouter>
    </QueryClientProvider>
  </StrictMode>,
)
