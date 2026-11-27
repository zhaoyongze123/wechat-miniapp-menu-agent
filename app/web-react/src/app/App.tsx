import { Suspense, lazy, type ReactNode } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from './AppShell'
import { ErrorBoundary } from './ErrorBoundary'
import { RoleProvider, useRole } from './RoleProvider'
import { UnknownRoutePage } from './UnknownRoutePage'
import { ToastProvider } from '../components/ToastProvider'
import { UnsavedChangesProvider } from '../components/UnsavedChangesProvider'
import { ProjectPageBoundary, ProjectProvider } from '../features/projects/ProjectProvider'
import type { AppRole } from '../api/types'
import { StatePanel } from '../components/StatePanel'

// 按路由代码分割：首屏只加载当前页面与公共壳，其余模块点击时按需加载。
const WorkbenchRoute = lazy(() => import('../features/worker/WorkbenchRoute').then((m) => ({ default: m.WorkbenchRoute })))
const ProjectsPage = lazy(() => import('../features/projects/ProjectsPage').then((m) => ({ default: m.ProjectsPage })))
const HubDevicesPage = lazy(() => import('../features/hub/HubDevicesPage').then((m) => ({ default: m.HubDevicesPage })))
const HubAssignmentsPage = lazy(() => import('../features/hub/HubAssignmentsPage').then((m) => ({ default: m.HubAssignmentsPage })))
const MaterialsPage = lazy(() => import('../features/materials/MaterialsPage').then((m) => ({ default: m.MaterialsPage })))
const QualityPage = lazy(() => import('../features/quality/QualityPage').then((m) => ({ default: m.QualityPage })))
const TaskCenterPage = lazy(() => import('../features/task-center/TaskCenterPage').then((m) => ({ default: m.TaskCenterPage })))
const SettingsPage = lazy(() => import('../features/settings/SettingsPage').then((m) => ({ default: m.SettingsPage })))
const AnnotationPage = lazy(() => import('../features/annotation/AnnotationPage').then((m) => ({ default: m.AnnotationPage })))
const DiagnosticsPage = lazy(() => import('../features/diagnostics/DiagnosticsPage').then((m) => ({ default: m.DiagnosticsPage })))

export function App() {
  return (
    <RoleProvider>
      <ToastProvider>
        <ProjectProvider>
          <UnsavedChangesProvider>
            <AppShell>
              <ErrorBoundary>
                <Suspense fallback={<StatePanel variant="loading" title="正在加载页面…" />}>
                  <Routes>
                  <Route path="/workbench" element={<ProjectBusinessRoute allow={['hub', 'worker']}><WorkbenchRoute /></ProjectBusinessRoute>} />
                  {/* 旧版 hash 深链保持可用：overview 与 environment 是仍会被收藏的入口。 */}
                  <Route path="/overview" element={<Navigate to="/workbench" replace />} />
                  <Route path="/environment" element={<Navigate to="/diagnostics" replace />} />
                  <Route path="/projects" element={<ProjectRoleRoute allow={['hub']}><ProjectsPage /></ProjectRoleRoute>} />
                  <Route path="/devices" element={<ProjectBusinessRoute allow={['hub']}><HubDevicesPage /></ProjectBusinessRoute>} />
                  <Route path="/assignments" element={<ProjectBusinessRoute allow={['hub']}><HubAssignmentsPage /></ProjectBusinessRoute>} />
                  <Route path="/materials" element={<ProjectBusinessRoute allow={['hub']}><MaterialsPage /></ProjectBusinessRoute>} />
                  <Route path="/rejections" element={<ProjectBusinessRoute allow={['hub']}><QualityPage /></ProjectBusinessRoute>} />
                  <Route path="/review-queue" element={<Navigate to="/rejections?view=review" replace />} />
                  <Route path="/task-center" element={<ProjectBusinessRoute allow={['hub', 'worker']}><TaskCenterPage /></ProjectBusinessRoute>} />
                  <Route path="/settings" element={<SettingsPage />} />
                  <Route path="/annotation" element={<ProjectBusinessRoute allow={['worker']}><AnnotationPage /></ProjectBusinessRoute>} />
                  <Route path="/diagnostics" element={<DiagnosticsPage />} />
                  {/* 首页回到采集工作台：与旧版默认入口 #overview 一致。 */}
                  <Route path="/" element={<Navigate to="/workbench" replace />} />
                  <Route path="*" element={<UnknownRoutePage />} />
                  </Routes>
                </Suspense>
              </ErrorBoundary>
            </AppShell>
          </UnsavedChangesProvider>
        </ProjectProvider>
      </ToastProvider>
    </RoleProvider>
  )
}

function ProjectRoleRoute({ allow, children }: { allow: AppRole[]; children: ReactNode }) {
  const role = useRole()
  if (role.loading) return <StatePanel variant="loading" title="正在确认运行角色…" />
  if (role.error) return <StatePanel variant="error" title="无法确认运行角色" description={role.error} />
  if (!allow.includes(role.role)) return <StatePanel variant="info" title="当前角色没有此页面的操作入口" description="请使用当前运行角色允许的页面。" />
  return <>{children}</>
}

function ProjectBusinessRoute({ allow, children }: { allow: AppRole[]; children: ReactNode }) {
  return <ProjectRoleRoute allow={allow}><ProjectPageBoundary>{children}</ProjectPageBoundary></ProjectRoleRoute>
}
