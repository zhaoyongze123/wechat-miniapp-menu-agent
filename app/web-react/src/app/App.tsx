import type { ReactNode } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from './AppShell'
import { ErrorBoundary } from './ErrorBoundary'
import { RoleProvider } from './RoleProvider'
import { UnknownRoutePage } from './UnknownRoutePage'
import { ToastProvider } from '../components/ToastProvider'
import { UnsavedChangesProvider } from '../components/UnsavedChangesProvider'
import { TaskCenterPage } from '../features/task-center/TaskCenterPage'
import { HubAssignmentsPage } from '../features/hub/HubAssignmentsPage'
import { HubDevicesPage } from '../features/hub/HubDevicesPage'
import { MaterialsPage } from '../features/materials/MaterialsPage'
import { QualityPage } from '../features/quality/QualityPage'
import { SettingsPage } from '../features/settings/SettingsPage'
import { DiagnosticsPage } from '../features/diagnostics/DiagnosticsPage'
import { AnnotationPage } from '../features/annotation/AnnotationPage'
import { WorkbenchRoute } from '../features/worker/WorkbenchRoute'
import { ProjectsPage } from '../features/projects/ProjectsPage'
import { ProjectPageBoundary, ProjectProvider } from '../features/projects/ProjectProvider'
import type { AppRole } from '../api/types'
import { useRole } from './RoleProvider'
import { StatePanel } from '../components/StatePanel'

export function App() {
  return (
    <RoleProvider>
      <ToastProvider>
        <ProjectProvider>
          <UnsavedChangesProvider>
            <AppShell>
              <ErrorBoundary>
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
