import type { ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { ROLE_LABEL, useRole } from './RoleProvider'
import type { AppRole } from '../api/types'
import { StatusBadge } from '../components/StatusBadge'

interface NavItem {
  key: string
  label: string
  roles: AppRole[]
  migrated?: boolean
}

/**
 * 导航只列出当前角色可见的入口。未迁移页面直接回到旧版同源 hash，
 * 已迁移页面留在 React 内部，不复制第二套路由规则。
 */
const NAV_ITEMS: NavItem[] = [
  { key: 'overview', label: '采集工作台', roles: ['hub', 'worker'] },
  { key: 'materials', label: '素材清洗', roles: ['hub'] },
  { key: 'rejections', label: '质量审核', roles: ['hub'] },
  { key: 'task-center', label: '任务中心', roles: ['hub', 'worker'], migrated: true },
  { key: 'settings', label: '设置', roles: ['hub', 'worker'] },
]

export function AppShell({ children }: { children: ReactNode }) {
  const role = useRole()
  const location = useLocation()
  const currentKey = location.pathname.replace(/^\//, '') || 'task-center'
  const visibleItems = NAV_ITEMS.filter((item) => item.roles.includes(role.role))
  const connection = connectionText(role.role, role.status)

  return (
    <div className="grid h-full grid-cols-1 overflow-hidden md:grid-cols-[212px_minmax(0,1fr)]">
      <aside className="flex min-h-0 flex-col border-b border-shell-line bg-shell-900 md:border-b-0 md:border-r">
        <div className="flex items-center gap-2.5 px-4 py-4">
          <span
            className="grid h-9 w-9 place-items-center rounded-lg bg-accent-500/15 text-sm font-bold text-accent-300"
            aria-hidden="true"
          >
            菜
          </span>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold">菜单采集工作台</p>
            <p className="text-[11px] uppercase tracking-[0.18em] text-shell-muted">React 控制台</p>
          </div>
        </div>

        <nav className="flex-1 space-y-1 overflow-y-auto px-2.5 py-2" aria-label="主导航">
          {visibleItems.map((item) => {
            const active = item.key === currentKey
            const className = [
              'flex items-center justify-between gap-2 rounded-lg px-3 py-2 text-sm transition-colors',
              active
                ? 'bg-accent-500/12 text-accent-200 border border-accent-500/30'
                : 'border border-transparent text-shell-muted hover:border-shell-line hover:bg-shell-850 hover:text-shell-text',
            ].join(' ')
            if (item.migrated) {
              return (
                <Link
                  key={item.key}
                  to={`/${item.key}`}
                  className={className}
                  aria-current={active ? 'page' : undefined}
                >
                  <span>{item.label}</span>
                  <span className="rounded border border-accent-500/30 px-1 text-[10px] text-accent-300">React</span>
                </Link>
              )
            }
            return (
              <a key={item.key} href={`/#${item.key}`} className={className} aria-current={active ? 'page' : undefined}>
                <span>{item.label}</span>
                <span className="text-[10px] text-shell-muted">旧版</span>
              </a>
            )
          })}
        </nav>

        <div className="space-y-2 border-t border-shell-line px-4 py-3 text-xs">
          <div className="flex items-center justify-between gap-2">
            <span className="text-shell-muted">当前角色</span>
            <StatusBadge tone={role.role === 'standalone' ? 'warning' : 'info'}>{ROLE_LABEL[role.role]}</StatusBadge>
          </div>
          <p className="break-words text-shell-muted">{connection}</p>
          <a className="inline-block text-accent-300 underline underline-offset-4" href="/#overview">
            返回旧版主页
          </a>
        </div>
      </aside>

      <div className="flex min-h-0 flex-col">
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-shell-line bg-shell-950/80 px-6 py-3 text-xs text-shell-muted backdrop-blur">
          <span>共享后台与 Windows 原生采集端</span>
          <span className="app-mono">一期迁移 · 任务中心试点</span>
        </header>
        <main id="app-main" className="min-h-0 flex-1 overflow-y-auto px-4 py-5 md:px-6">
          <div className="mx-auto w-full max-w-[1400px]">{children}</div>
        </main>
      </div>
    </div>
  )
}

function connectionText(role: AppRole, status: ReturnType<typeof useRole>['status']): string {
  if (!status) return '正在读取连接状态…'
  if (status.role === 'hub') {
    return `已配对设备 ${status.workers.length} 台`
  }
  if (status.role === 'worker') {
    if (!status.paired) return '尚未配对共享后台'
    if (status.credential_invalid) return '配对凭证已失效，请联系管理员'
    if (!status.online) {
      const pending = status.pending_uploads ? `，${status.pending_uploads} 个结果待补传` : ''
      return `共享后台离线${pending}`
    }
    return `已连接共享后台${status.name ? ` · ${status.name}` : ''}`
  }
  return role === 'standalone' ? '未启用共享角色' : ''
}
