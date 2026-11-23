import { Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from './AppShell'
import { ErrorBoundary } from './ErrorBoundary'
import { RoleProvider } from './RoleProvider'
import { UnknownRoutePage } from './UnknownRoutePage'
import { ToastProvider } from '../components/ToastProvider'
import { TaskCenterPage } from '../features/task-center/TaskCenterPage'
import { HubAssignmentsPage } from '../features/hub/HubAssignmentsPage'
import { HubDevicesPage } from '../features/hub/HubDevicesPage'
import { HubWorkbenchPage } from '../features/hub/HubWorkbenchPage'

export function App() {
  return (
    <RoleProvider>
      <ToastProvider>
        <AppShell>
          <ErrorBoundary>
            <Routes>
              <Route path="/workbench" element={<HubWorkbenchPage />} />
              <Route path="/devices" element={<HubDevicesPage />} />
              <Route path="/assignments" element={<HubAssignmentsPage />} />
              <Route path="/task-center" element={<TaskCenterPage />} />
              <Route path="/" element={<Navigate to="/task-center" replace />} />
              <Route path="*" element={<UnknownRoutePage />} />
            </Routes>
          </ErrorBoundary>
        </AppShell>
      </ToastProvider>
    </RoleProvider>
  )
}
