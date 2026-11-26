import { createContext, Fragment, useCallback, useContext, useEffect, useMemo, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { StatePanel } from '../../components/StatePanel'
import { useRole } from '../../app/RoleProvider'
import { fetchProjects, projectsKeys, type Project } from './api'

interface ProjectContextValue {
  projectId: string
  project: Project | null
  displayProject: Project | null
  projects: Project[]
  loading: boolean
  error: string
  invalid: boolean
  isBusinessPage: boolean
  switchHref: (projectId: string) => string
}

const ProjectContext = createContext<ProjectContextValue | null>(null)
const PROJECT_PREFERENCE_PREFIX = 'menu-capture.project-preference.v1'
const BUSINESS_PATHS = new Set([
  '/workbench', '/materials', '/rejections', '/task-center', '/assignments', '/annotation', '/devices',
])

function preferenceKey(role: string, instanceName?: string): string {
  // 本机服务同源代表实例；Worker 再按已配对 Hub 名称区分本地偏好。
  return `${PROJECT_PREFERENCE_PREFIX}:${window.location.origin}:${role}:${instanceName ?? ''}`
}

function readPreference(key: string): string {
  try {
    return window.localStorage.getItem(key) ?? ''
  } catch {
    return ''
  }
}

function rememberPreference(key: string, projectId: string): void {
  try {
    window.localStorage.setItem(key, projectId)
  } catch {
    // 偏好记忆不可用时，URL 和服务器仍是项目归属事实源。
  }
}

export function ProjectProvider({ children }: { children: ReactNode }) {
  const role = useRole()
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const isBusinessPage = BUSINESS_PATHS.has(location.pathname)
  const catalogueScope = role.role === 'worker' ? `worker:${role.status?.role === 'worker' ? role.status.name : ''}` : role.role
  const listQuery = useQuery({
    queryKey: projectsKeys.list(true, catalogueScope),
    queryFn: ({ signal }) => fetchProjects(true, signal),
    enabled: !role.loading && !role.error,
    staleTime: 20_000,
  })
  const projects = listQuery.data ?? []
  const requestedId = searchParams.get('project') ?? ''
  const key = preferenceKey(role.role, role.status?.role === 'worker' ? role.status.name : undefined)
  const preferredId = readPreference(key)
  const project = requestedId ? projects.find((item) => item.id === requestedId) ?? null : null

  const defaultProject = useMemo(() => {
    const preferred = projects.find((item) => item.id === preferredId)
    if (preferred) return preferred
    return projects.find((item) => item.code === 'legacy') ?? projects.find((item) => !item.archived_at) ?? null
  }, [preferredId, projects])

  useEffect(() => {
    if (requestedId && project) rememberPreference(key, requestedId)
  }, [key, project, requestedId])

  useEffect(() => {
    if (!isBusinessPage || requestedId || listQuery.isPending || listQuery.isError) return
    if (!defaultProject) return
    const next = new URLSearchParams(searchParams)
    next.set('project', defaultProject.id)
    setSearchParams(next, { replace: true })
  }, [defaultProject, isBusinessPage, listQuery.isError, listQuery.isPending, requestedId, searchParams, setSearchParams])

  const switchHref = useCallback((nextProjectId: string) => {
    const current = new URLSearchParams(location.search)
    const params = new URLSearchParams()
    params.set('project', nextProjectId)
    // 切换后丢弃旧批次、详情和分页参数；质量审核的视图标签不依赖具体项目。
    if (location.pathname === '/rejections' && current.has('view')) params.set('view', current.get('view') ?? '')
    return `${location.pathname}?${params.toString()}`
  }, [location.pathname, location.search])

  const displayProject = project ?? (!isBusinessPage ? defaultProject : null)
  const value = useMemo<ProjectContextValue>(() => ({
    projectId: displayProject?.id ?? '',
    project: displayProject,
    displayProject,
    projects,
    loading: listQuery.isPending || role.loading,
    error: listQuery.error instanceof Error ? listQuery.error.message : role.error,
    invalid: Boolean(requestedId && !listQuery.isPending && !project),
    isBusinessPage,
    switchHref,
  }), [displayProject, isBusinessPage, listQuery.error, listQuery.isPending, projects, requestedId, role.error, role.loading, switchHref])

  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>
}

export function useProject(): ProjectContextValue {
  const value = useContext(ProjectContext)
  if (!value) throw new Error('useProject 必须在 ProjectProvider 内使用')
  return value
}

/** 项目业务页统一处理加载、错误、无项目与失效深链。归档项目保留读取能力。 */
export function ProjectPageBoundary({ children }: { children: ReactNode }) {
  const { project, projectId, loading, error, invalid } = useProject()
  const role = useRole()
  const navigate = useNavigate()
  if (loading) return <StatePanel variant="loading" title="正在读取项目…" />
  if (error) return <StatePanel variant="error" title="无法读取项目列表" description={error} />
  if (invalid) {
    return (
      <StatePanel
        variant="error"
        title="链接中的项目不可用"
        description="该项目不存在，或当前设备没有该项目的本地记录。请从项目列表选择有效项目。"
        action={<Button color="secondary" variant="outline" size="sm" onClick={() => navigate(role.role === 'hub' ? '/projects' : '/workbench')}>选择项目</Button>}
      />
    )
  }
  if (!projectId || !project) {
    return (
      <StatePanel
        variant="empty"
        title={role.role === 'hub' ? '还没有项目' : '本机尚无项目记录'}
        description={role.role === 'hub' ? '创建项目后再导入素材、分配和查看业务记录。' : '同步或接收项目分配后，Worker 才会显示可选项目。'}
        action={role.role === 'hub' ? <Button color="primary" size="sm" onClick={() => navigate('/projects')}>创建项目</Button> : undefined}
      />
    )
  }
  // 项目切换时卸载旧页树，避免旧请求的完成回调改写新项目的表单、选择或弹窗状态。
  return (
    <Fragment key={projectId}>
      {project.archived_at ? <Alert color="warning" variant="soft" title="项目已归档" description="历史数据仍可查看，新增业务写入将被服务端拒绝。" /> : null}
      {children}
    </Fragment>
  )
}
