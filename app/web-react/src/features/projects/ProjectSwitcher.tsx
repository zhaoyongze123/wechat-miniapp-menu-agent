import { Link } from 'react-router-dom'
import { StatusBadge } from '../../components/StatusBadge'
import { useRole } from '../../app/RoleProvider'
import { useProject } from './ProjectProvider'

/** 项目切换使用路由链接，既保留深链语义，也由全局未保存守卫统一确认。 */
export function ProjectSwitcher() {
  const { role } = useRole()
  const { project, projects, loading, error, isBusinessPage, switchHref } = useProject()
  if (role === 'standalone' || (!isBusinessPage && !project)) return null

  if (!isBusinessPage) {
    return (
      <span className="hidden max-w-56 truncate text-xs text-tertiary lg:inline" title="设备、设置和诊断仍按本机实例管理">
        当前项目：{project?.name ?? (loading ? '读取中…' : error ? '读取失败' : '无项目')} · 页面按实例管理
      </span>
    )
  }

  return (
    <details className="relative">
      <summary
        className="max-w-56 cursor-pointer truncate rounded-md border border-subtle bg-surface-secondary px-2.5 py-1.5 text-xs text-default focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-ring)]"
        aria-label="切换项目"
        title="切换项目"
      >
        项目：{project?.name ?? (loading ? '读取中…' : error ? '读取失败' : '未选择')}
        {project?.archived_at ? '（已归档）' : ''}
      </summary>
      <div className="absolute right-0 z-30 mt-2 max-h-80 w-72 overflow-auto rounded-xl border border-subtle bg-surface p-2 shadow-md">
        {loading ? <p className="px-2 py-2 text-xs text-secondary">正在读取项目…</p> : null}
        {error ? <p role="alert" className="px-2 py-2 text-xs text-danger">{error}</p> : null}
        {!loading && !error && projects.length === 0 ? <p className="px-2 py-2 text-xs text-secondary">没有可选项目</p> : null}
        <ul className="space-y-1">
          {projects.map((item) => (
            <li key={item.id}>
              <Link
                to={switchHref(item.id)}
                aria-current={item.id === project?.id ? 'page' : undefined}
                className="flex items-center justify-between gap-2 rounded-md px-2 py-2 text-sm text-default hover:bg-secondary-soft focus-visible:outline-2 focus-visible:outline-[var(--color-ring)]"
              >
                <span className="min-w-0">
                  <span className="block truncate">{item.name}</span>
                  <span className="block truncate text-2xs text-tertiary">{item.code}</span>
                </span>
                {item.archived_at ? <StatusBadge tone="secondary">已归档</StatusBadge> : null}
              </Link>
            </li>
          ))}
        </ul>
      </div>
    </details>
  )
}
