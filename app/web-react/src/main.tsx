import { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import { AppsSDKUIProvider } from '@openai/apps-sdk-ui/components/AppsSDKUIProvider'
import { HashRouter } from 'react-router-dom'
import './styles/index.css'
import { App } from './app/App'
import { queryClient } from './api/query'
import { ToastProvider } from './components/ToastProvider'
import { NotificationRuntime } from './features/notifications/NotificationRuntime'

const RemoteControlPage = lazy(() =>
  import('./features/manual-control/RemoteControlPage').then((m) => ({ default: m.RemoteControlPage })),
)

const container = document.getElementById('root')
if (!container) throw new Error('缺少 React 挂载节点')

// 旧版入口使用 `/#task-center` 这类 hash：HashRouter 需要 `#/path`，
// 在首次渲染前归一化，地址栏只改一次、不新增历史记录。
if (window.location.hash.length > 1 && !window.location.hash.startsWith('#/')) {
  const normalized = `#/${window.location.hash.slice(1)}`
  window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}${normalized}`)
}

createRoot(container).render(
  <StrictMode>
    <AppsSDKUIProvider linkComponent="a">
      <QueryClientProvider client={queryClient}>
        {window.location.pathname === '/remote-control' ? (
          <ToastProvider>
            <Suspense fallback={null}>
              <RemoteControlPage />
            </Suspense>
            <NotificationRuntime />
          </ToastProvider>
        ) : (
          <HashRouter>
            <App />
          </HashRouter>
        )}
      </QueryClientProvider>
    </AppsSDKUIProvider>
  </StrictMode>,
)
