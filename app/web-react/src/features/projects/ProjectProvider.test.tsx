import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ProjectPageBoundary, ProjectProvider, useProject } from './ProjectProvider'
import { fetchProjects } from './api'
import type { Project } from './api'

const roleState = vi.hoisted(() => ({ role: 'hub' as 'hub' | 'worker' | 'standalone' }))
vi.mock('../../app/RoleProvider', () => ({
  useRole: () => ({ role: roleState.role, loading: false, error: '', status: { role: roleState.role } }),
}))
vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, fetchProjects: vi.fn() }
})

const projects: Project[] = [
  { id: 'project-a', code: 'legacy', name: '旧项目', description: '', created_at: '', updated_at: '', archived_at: '' },
  { id: 'project-b', code: 'q4', name: '第四季度', description: '', created_at: '', updated_at: '', archived_at: '' },
]

function Probe() {
  const { projectId, switchHref } = useProject()
  const location = useLocation()
  return <output data-testid="selected-project" data-switch-href={switchHref('project-a')} data-search={location.search}>{projectId}</output>
}

function renderProvider(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>
          <Probe />
          <ProjectPageBoundary><div>业务内容</div></ProjectPageBoundary>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ProjectProvider', () => {
  beforeEach(() => {
    roleState.role = 'hub'
    localStorage.clear()
    vi.mocked(fetchProjects).mockReset()
    vi.mocked(fetchProjects).mockResolvedValue(projects)
  })

  it('uses the project ID in a deep link as the selected project', async () => {
    renderProvider('/materials?project=project-b&batch=stale')
    await waitFor(() => expect(screen.getByTestId('selected-project')).toHaveTextContent('project-b'))
    expect(screen.getByTestId('selected-project')).toHaveAttribute('data-switch-href', '/materials?project=project-a')
  })

  it('uses a preference only as the default and writes that choice into the URL', async () => {
    const key = `menu-capture.project-preference.v1:${window.location.origin}:hub:`
    localStorage.setItem(key, 'project-b')
    renderProvider('/materials')
    await waitFor(() => expect(screen.getByTestId('selected-project')).toHaveTextContent('project-b'))
    await waitFor(() => expect(screen.getByTestId('selected-project')).toHaveAttribute('data-search', '?project=project-b'))
  })

  it('treats the device page as project scoped and writes its default project into the URL', async () => {
    renderProvider('/devices')
    await waitFor(() => expect(screen.getByTestId('selected-project')).toHaveTextContent('project-a'))
    await waitFor(() => expect(screen.getByTestId('selected-project')).toHaveAttribute('data-search', '?project=project-a'))
  })

  it('rejects unknown explicit project IDs instead of silently falling back', async () => {
    renderProvider('/materials?project=missing')
    expect(await screen.findByText('链接中的项目不可用')).toBeInTheDocument()
    expect(screen.queryByText('业务内容')).not.toBeInTheDocument()
  })

  it('keeps the standalone legacy workspace available to local business pages', async () => {
    roleState.role = 'standalone'
    vi.mocked(fetchProjects).mockResolvedValue(projects.slice(0, 1))
    renderProvider('/materials')
    await waitFor(() => expect(screen.getByTestId('selected-project')).toHaveTextContent('project-a'))
    expect(await screen.findByText('业务内容')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('selected-project')).toHaveAttribute('data-search', '?project=project-a'))
  })
})
