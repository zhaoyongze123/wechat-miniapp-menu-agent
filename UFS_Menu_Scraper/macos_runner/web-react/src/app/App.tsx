import { Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from './AppShell'
import { ErrorBoundary } from './ErrorBoundary'
import { RoleProvider } from './RoleProvider'
import { UnknownRoutePage } from './UnknownRoutePage'
import { ToastProvider } from '../components/ToastProvider'
import { TaskCenterPage } from '../features/task-center/TaskCenterPage'

export function App() {
  return (
    <RoleProvider>
      <ToastProvider>
        <AppShell>
          <ErrorBoundary>
            <Routes>
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
