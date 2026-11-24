import { useEffect, useState, type ComponentType, type ReactNode, type SVGProps } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Avatar } from '@openai/apps-sdk-ui/components/Avatar'
import { Badge } from '@openai/apps-sdk-ui/components/Badge'
import {
  CheckCircle,
  Clipboard,
  Cursor,
  Desktop,
  Home,
  Images,
  Settings,
  Tasks,
} from '@openai/apps-sdk-ui/components/Icon'
import { ROLE_LABEL, useRole } from './RoleProvider'
import type { AppRole } from '../api/types'
import { fetchActiveTasks } from '../features/task-center/api'
import { NotificationRuntime } from '../features/notifications/NotificationRuntime'

interface NavItem {
  key: string
  label: string
  roles: AppRole[]
  icon: ComponentType<SVGProps<SVGSVGElement>>
}

/**
 * 导航只列出当前角色可见的入口；所有页面都在 React 路由内，
 * 不再回退到旧版同源 hash。
 */
const NAV_ITEMS: NavItem[] = [
  { key: 'workbench', label: '采集工作台', roles: ['hub', 'worker'], icon: Home },
  { key: 'annotation', label: '人工标注', roles: ['worker'], icon: Cursor },
  { key: 'devices', label: '采集电脑', roles: ['hub'], icon: Desktop },
  { key: 'assignments', label: '分配记录', roles: ['hub'], icon: Clipboard },
  { key: 'materials', label: '素材清洗', roles: ['hub'], icon: Images },
  { key: 'rejections', label: '质量审核', roles: ['hub'], icon: CheckCircle },
  { key: 'task-center', label: '任务中心', roles: ['hub', 'worker'], icon: Tasks },
  { key: 'settings', label: '设置', roles: ['hub', 'worker'], icon: Settings },
]

const SIDEBAR_COLLAPSED_KEY = 'menu-capture.sidebar-collapsed'

export function AppShell({ children }: { children: ReactNode }) {
  const role = useRole()
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1'
    } catch {
      return false
    }
  })
  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? '1' : '0')
    } catch {
      // 浏览器禁止本地存储时只影响偏好记忆，不阻塞导航。
    }
  }, [collapsed])
  const currentKey = location.pathname.replace(/^\//, '') || 'task-center'
  const visibleItems = NAV_ITEMS.filter((item) => item.roles.includes(role.role))
  const currentItem = NAV_ITEMS.find((item) => item.key === currentKey)
  // 加载中或读取失败时不能把未知角色显示成“本机独立模式”。
  const connection = role.loading ? '正在读取连接状态…' : role.error ? '角色状态读取失败' : connectionText(role.role, role.status)

  return (
    <div className="flex h-full overflow-hidden bg-surface text-default">
      <NotificationRuntime />
      <aside
        className={[
          'hidden shrink-0 flex-col overflow-hidden bg-surface-secondary md:flex',
          collapsed ? 'w-16' : 'w-64',
        ].join(' ')}
      >
        <div className={['flex items-center gap-2.5 px-4 py-4', collapsed ? 'justify-center px-2' : ''].join(' ')}>
          <img src="/favicon.svg" alt="" width={32} height={32} className="h-8 w-8 shrink-0" />
          {!collapsed ? (
            <div className="min-w-0 flex-1">
              <p className="truncate heading-xs">菜单采集工作台</p>
              <p className="truncate text-2xs text-tertiary">共享后台 · Windows 采集端</p>
            </div>
          ) : null}
          <button
            type="button"
            onClick={() => setCollapsed((value) => !value)}
            aria-label={collapsed ? '展开侧栏' : '折叠侧栏'}
            aria-expanded={!collapsed}
            title={collapsed ? '展开侧栏' : '折叠侧栏'}
            className="shrink-0 rounded-md border border-subtle px-1.5 py-0.5 text-xs text-secondary hover:bg-secondary-ghost-hover"
          >
            {collapsed ? '»' : '«'}
          </button>
        </div>

        <nav className="flex-1 space-y-0.5 overflow-y-auto px-2" aria-label="主导航">
          {visibleItems.map((item) => (
            <NavLink key={item.key} item={item} active={item.key === currentKey} iconOnly={collapsed} />
          ))}
        </nav>

        <div className="border-t border-subtle p-2">
          <div className={['flex items-center gap-2.5 rounded-lg px-2 py-2', collapsed ? 'justify-center' : ''].join(' ')}>
            <Avatar size={28} name={ROLE_LABEL[role.role]} color="secondary" />
            {!collapsed ? (
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm text-default">{ROLE_LABEL[role.role]}</span>
                <span className="block truncate text-2xs text-tertiary">{connection}</span>
              </span>
            ) : null}
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-subtle bg-surface px-4 md:px-6">
          <div className="flex min-w-0 items-center gap-2 md:hidden">
            <img src="/favicon.svg" alt="" width={24} height={24} className="h-6 w-6 shrink-0" />
            <h2 className="truncate heading-xs">{currentItem?.label ?? '菜单采集工作台'}</h2>
          </div>
          <div className="ml-auto flex shrink-0 items-center gap-2.5">
            {currentKey === 'task-center' ? null : <ActiveTaskIndicator />}
            <span className="hidden text-xs text-tertiary sm:inline">{connection}</span>
            <Badge color={role.role === 'standalone' ? 'warning' : 'secondary'} variant="soft" size="sm">
              {role.loading ? '正在确认角色' : role.error ? '角色未知' : ROLE_LABEL[role.role]}
            </Badge>
          </div>
        </header>

        <nav
          className="flex shrink-0 gap-1 overflow-x-auto border-b border-subtle bg-surface px-3 py-2 md:hidden"
          aria-label="主导航（移动端）"
        >
          {visibleItems.map((item) => (
            <NavLink key={item.key} item={item} active={item.key === currentKey} compact />
          ))}
        </nav>

        <main id="app-main" className="min-h-0 flex-1 overflow-y-auto bg-surface px-4 py-6 md:px-10">
          <div className="mx-auto w-full max-w-6xl">{children}</div>
        </main>
      </div>
    </div>
  )
}

function NavLink({
  item,
  active,
  compact = false,
  iconOnly = false,
}: {
  item: NavItem
  active: boolean
  compact?: boolean
  iconOnly?: boolean
}) {
  const Icon = item.icon
  const className = [
    'flex items-center gap-2.5 rounded-sm px-2 py-1.5 text-sm transition-colors',
    compact ? 'shrink-0 whitespace-nowrap' : '',
    iconOnly ? 'justify-center' : '',
    active
      ? 'bg-secondary-soft text-default'
      : 'text-secondary hover:bg-secondary-ghost-hover hover:text-default',
  ].join(' ')
  const content = (
    <>
      <Icon width={compact ? 15 : 17} height={compact ? 15 : 17} className="shrink-0" />
      {iconOnly ? null : item.label}
    </>
  )
  return (
    <Link
      to={`/${item.key}`}
      className={className}
      aria-current={active ? 'page' : undefined}
      aria-label={iconOnly ? item.label : undefined}
      title={iconOnly ? item.label : undefined}
    >
      {content}
    </Link>
  )
}

/** 全局运行中任务入口：任何页面都能看到正在执行的采集或素材分析。 */
function ActiveTaskIndicator() {
  const query = useQuery({
    queryKey: ['tasks-active'],
    queryFn: ({ signal }) => fetchActiveTasks(signal),
    refetchInterval: 5_000,
    staleTime: 4_000,
  })
  const active = query.data?.tasks.filter((task) => !['completed', 'failed', 'terminated', 'cancelled'].includes(task.status)) ?? []
  if (!active.length) return null
  return (
    <details className="relative">
      <summary className="cursor-pointer rounded-full border border-subtle bg-secondary-soft px-2.5 py-1 text-xs">
        运行中 {active.length} 个任务
      </summary>
      <ul className="absolute right-0 top-full z-20 mt-2 max-h-72 w-64 overflow-auto rounded-lg border border-subtle bg-surface p-2 shadow-md">
        {active.map((task) => (
          <li key={`${task.source}:${task.id}`}>
            <Link
              to={`/task-center?${new URLSearchParams({ detail: `${task.source}:${task.id}` })}`}
              className="block rounded px-2 py-2 text-sm hover:bg-secondary-soft"
            >
              <span className="block break-words">{task.title}</span>
              <span className="text-xs text-secondary">{task.progress.done} / {task.progress.total}</span>
            </Link>
          </li>
        ))}
      </ul>
    </details>
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
