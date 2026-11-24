import { Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from './AppShell'
import { ErrorBoundary } from './ErrorBoundary'
import { RoleProvider } from './RoleProvider'
import { UnknownRoutePage } from './UnknownRoutePage'
import { ToastProvider } from '../components/ToastProvider'
import { TaskCenterPage } from '../features/task-center/TaskCenterPage'
import { HubAssignmentsPage } from '../features/hub/HubAssignmentsPage'
import { HubDevicesPage } from '../features/hub/HubDevicesPage'
import { MaterialsPage } from '../features/materials/MaterialsPage'
import { QualityPage } from '../features/quality/QualityPage'
import { SettingsPage } from '../features/settings/SettingsPage'
import { DiagnosticsPage } from '../features/diagnostics/DiagnosticsPage'
import { AnnotationPage } from '../features/annotation/AnnotationPage'
import { WorkbenchRoute } from '../features/worker/WorkbenchRoute'

export function App() {
  return (
    <RoleProvider>
      <ToastProvider>
        <AppShell>
          <ErrorBoundary>
            <Routes>
              <Route path="/workbench" element={<WorkbenchRoute />} />
              <Route path="/devices" element={<HubDevicesPage />} />
              <Route path="/assignments" element={<HubAssignmentsPage />} />
              <Route path="/materials" element={<MaterialsPage />} />
              <Route path="/rejections" element={<QualityPage />} />
              <Route path="/review-queue" element={<Navigate to="/rejections?view=review" replace />} />
              <Route path="/task-center" element={<TaskCenterPage />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="/annotation" element={<AnnotationPage />} />
              <Route path="/diagnostics" element={<DiagnosticsPage />} />
              <Route path="/" element={<Navigate to="/task-center" replace />} />
              <Route path="*" element={<UnknownRoutePage />} />
            </Routes>
          </ErrorBoundary>
        </AppShell>
      </ToastProvider>
    </RoleProvider>
  )
}
