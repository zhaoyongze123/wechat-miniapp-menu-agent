import type { ComponentType, ReactNode, SVGProps } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { Avatar } from '@openai/apps-sdk-ui/components/Avatar'
import { Badge } from '@openai/apps-sdk-ui/components/Badge'
import { Menu } from '@openai/apps-sdk-ui/components/Menu'
import {
  CheckCircle,
  ChevronRight,
  Clipboard,
  Desktop,
  Home,
  Images,
  Settings,
  Tasks,
} from '@openai/apps-sdk-ui/components/Icon'
import { ROLE_LABEL, useRole } from './RoleProvider'
import type { AppRole } from '../api/types'

interface NavItem {
  key: string
  label: string
  roles: AppRole[]
  icon: ComponentType<SVGProps<SVGSVGElement>>
  /** 所有角色都已迁移到 React。 */
  migrated?: boolean
  /** 仅列出的角色走 React 路由，其余角色仍回旧版 hash。 */
  migratedFor?: AppRole[]
  /** 旧版入口 hash 与 React 路由 key 不同的页面在此声明。 */
  legacyHash?: string
}

/**
 * 导航只列出当前角色可见的入口。未迁移页面直接回到旧版同源 hash，
 * 已迁移页面留在 React 内部，不复制第二套路由规则。
 */
const NAV_ITEMS: NavItem[] = [
  { key: 'workbench', label: '采集工作台', roles: ['hub', 'worker'], icon: Home, migratedFor: ['hub', 'worker'], legacyHash: 'overview' },
  { key: 'devices', label: '采集电脑', roles: ['hub'], icon: Desktop, migrated: true },
  { key: 'assignments', label: '分配记录', roles: ['hub'], icon: Clipboard, migrated: true },
  { key: 'materials', label: '素材清洗', roles: ['hub'], icon: Images, migrated: true },
  { key: 'rejections', label: '质量审核', roles: ['hub'], icon: CheckCircle, migrated: true },
  { key: 'task-center', label: '任务中心', roles: ['hub', 'worker'], icon: Tasks, migrated: true },
  { key: 'settings', label: '设置', roles: ['hub', 'worker'], icon: Settings },
]

export function AppShell({ children }: { children: ReactNode }) {
  const role = useRole()
  const location = useLocation()
  const currentKey = location.pathname.replace(/^\//, '') || 'task-center'
  const visibleItems = NAV_ITEMS.filter((item) => item.roles.includes(role.role))
  const currentItem = NAV_ITEMS.find((item) => item.key === currentKey)
  const connection = connectionText(role.role, role.status)

  return (
    <div className="flex h-full overflow-hidden bg-surface text-default">
      <aside className="hidden w-64 shrink-0 flex-col overflow-hidden bg-surface-secondary md:flex">
        <div className="flex items-center gap-2.5 px-4 py-4">
          <span
            className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-primary-solid text-xs font-semibold text-inverse"
            aria-hidden="true"
          >
            菜
          </span>
          <div className="min-w-0">
            <p className="truncate heading-xs">菜单采集工作台</p>
            <p className="truncate text-2xs text-tertiary">共享后台 · Windows 采集端</p>
          </div>
        </div>

        <nav className="flex-1 space-y-0.5 overflow-y-auto px-2" aria-label="主导航">
          {visibleItems.map((item) => (
            <NavLink
              key={item.key}
              item={item}
              active={item.key === currentKey}
              migrated={item.migrated === true || item.migratedFor?.includes(role.role) === true}
            />
          ))}
        </nav>

        <div className="border-t border-subtle p-2">
          <Menu>
            <Menu.Trigger>
              <button
                type="button"
                className="flex w-full items-center gap-2.5 rounded-lg px-2 py-2 text-left transition-colors hover:bg-secondary-ghost-hover"
              >
                <Avatar size={28} name={ROLE_LABEL[role.role]} color="secondary" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm text-default">{ROLE_LABEL[role.role]}</span>
                  <span className="block truncate text-2xs text-tertiary">{connection}</span>
                </span>
                <ChevronRight width={14} height={14} className="shrink-0 -rotate-90 text-tertiary" />
              </button>
            </Menu.Trigger>
            <Menu.Content side="top" align="start" width={224} minWidth={224}>
              <Menu.Item onSelect={() => { window.location.href = '/#overview' }}>返回旧版主页</Menu.Item>
              <Menu.Item onSelect={() => { window.location.href = '/#task-center' }}>旧版控制台</Menu.Item>
            </Menu.Content>
          </Menu>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-subtle bg-surface px-4 md:px-6">
          <div className="flex min-w-0 items-center gap-2 md:hidden">
            <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-primary-solid text-2xs font-semibold text-inverse">
              菜
            </span>
            <h2 className="truncate heading-xs">{currentItem?.label ?? '菜单采集工作台'}</h2>
          </div>
          <div className="ml-auto flex shrink-0 items-center gap-2.5">
            <span className="hidden text-xs text-tertiary sm:inline">{connection}</span>
            <Badge color={role.role === 'standalone' ? 'warning' : 'secondary'} variant="soft" size="sm">
              {ROLE_LABEL[role.role]}
            </Badge>
          </div>
        </header>

        <nav
          className="flex shrink-0 gap-1 overflow-x-auto border-b border-subtle bg-surface px-3 py-2 md:hidden"
          aria-label="主导航（移动端）"
        >
          {visibleItems.map((item) => (
            <NavLink
              key={item.key}
              item={item}
              active={item.key === currentKey}
              migrated={item.migrated === true || item.migratedFor?.includes(role.role) === true}
              compact
            />
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
  migrated,
  compact = false,
}: {
  item: NavItem
  active: boolean
  migrated: boolean
  compact?: boolean
}) {
  const Icon = item.icon
  const className = [
    'flex items-center gap-2.5 rounded-sm px-2 py-1.5 text-sm transition-colors',
    compact ? 'shrink-0 whitespace-nowrap' : '',
    active
      ? 'bg-secondary-soft text-default'
      : 'text-secondary hover:bg-secondary-ghost-hover hover:text-default',
  ].join(' ')
  const content = (
    <>
      <Icon width={compact ? 15 : 17} height={compact ? 15 : 17} className="shrink-0" />
      {item.label}
    </>
  )
  if (migrated) {
    return (
      <Link to={`/${item.key}`} className={className} aria-current={active ? 'page' : undefined}>
        {content}
      </Link>
    )
  }
  return (
    <a href={`/#${item.legacyHash ?? item.key}`} className={className} aria-current={active ? 'page' : undefined}>
      {content}
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
