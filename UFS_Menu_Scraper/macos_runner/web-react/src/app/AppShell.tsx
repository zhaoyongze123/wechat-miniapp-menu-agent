import type { ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { Badge } from '@openai/apps-sdk-ui/components/Badge'
import { TextLink } from '@openai/apps-sdk-ui/components/TextLink'
import { ROLE_LABEL, useRole } from './RoleProvider'
import type { AppRole } from '../api/types'

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
  const currentItem = NAV_ITEMS.find((item) => item.key === currentKey)
  const connection = connectionText(role.role, role.status)

  return (
    <div className="flex h-full overflow-hidden bg-canvas text-ink">
      <aside className="hidden w-[248px] shrink-0 flex-col bg-canvas md:flex">
        <div className="flex items-center gap-2.5 px-4 py-5">
          <span
            className="grid h-9 w-9 place-items-center rounded-full bg-ink text-[13px] font-semibold text-[var(--color-text-inverse)]"
            aria-hidden="true"
          >
            菜
          </span>
          <div className="min-w-0">
            <p className="truncate text-[15px] font-semibold">菜单采集工作台</p>
            <p className="truncate text-xs text-ink-faint">共享后台 · Windows 采集端</p>
          </div>
        </div>

        <nav className="flex-1 space-y-0.5 overflow-y-auto px-2 pb-2" aria-label="主导航">
          {visibleItems.map((item) => (
            <NavLink key={item.key} item={item} active={item.key === currentKey} />
          ))}
        </nav>

        <div className="space-y-2 border-t border-line-subtle px-4 py-3 text-xs">
          <div className="flex items-center justify-between gap-2">
            <span className="text-ink-muted">当前角色</span>
            <Badge color={role.role === 'standalone' ? 'warning' : 'secondary'} variant="soft" size="sm">
              {ROLE_LABEL[role.role]}
            </Badge>
          </div>
          <p className="leading-relaxed break-words text-ink-faint">{connection}</p>
          <TextLink href="/#overview" className="text-xs">
            返回旧版主页
          </TextLink>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-line-subtle bg-panel px-4 md:px-6">
          <div className="flex min-w-0 items-center gap-2">
            <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-ink text-[11px] font-semibold text-[var(--color-text-inverse)] md:hidden">
              菜
            </span>
            <h2 className="truncate text-[15px] font-semibold">{currentItem?.label ?? '菜单采集工作台'}</h2>
          </div>
          <div className="flex shrink-0 items-center gap-3 text-xs text-ink-muted">
            <span className="hidden sm:inline">{connection}</span>
            <TextLink href="/#task-center" className="text-xs">
              旧版控制台
            </TextLink>
          </div>
        </header>

        <nav
          className="flex shrink-0 gap-1 overflow-x-auto border-b border-line-subtle bg-panel px-3 py-2 md:hidden"
          aria-label="主导航（移动端）"
        >
          {visibleItems.map((item) => (
            <NavLink key={item.key} item={item} active={item.key === currentKey} compact />
          ))}
        </nav>

        <main id="app-main" className="min-h-0 flex-1 overflow-y-auto bg-canvas px-4 py-6 md:px-8">
          <div className="mx-auto w-full max-w-[1200px]">{children}</div>
        </main>
      </div>
    </div>
  )
}

function NavLink({ item, active, compact = false }: { item: NavItem; active: boolean; compact?: boolean }) {
  const className = [
    'flex items-center rounded-lg px-3 py-2 text-sm transition-colors',
    compact ? 'shrink-0 whitespace-nowrap' : '',
    active
      ? 'bg-[var(--alpha-08)] font-medium text-ink'
      : 'text-ink-muted hover:bg-[var(--alpha-04)] hover:text-ink',
  ].join(' ')
  if (item.migrated) {
    return (
      <Link to={`/${item.key}`} className={className} aria-current={active ? 'page' : undefined}>
        {item.label}
      </Link>
    )
  }
  return (
    <a href={`/#${item.key}`} className={className} aria-current={active ? 'page' : undefined}>
      {item.label}
    </a>
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
