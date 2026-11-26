import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchTaskActions, fetchTaskArtifacts } from './api'
import { WorkerTaskDetailDrawer } from './WorkerTaskDetailDrawer'
import type { WorkerAction } from './api'
import type { WorkerTask } from './types'

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, fetchTaskActions: vi.fn(), fetchTaskArtifacts: vi.fn() }
})
vi.mock('../projects/ProjectProvider', () => ({ useProject: () => ({ projectId: 'project-test' }) }))

const task: WorkerTask = {
  id: '0123456789abcdef0123',
  source_name: 'E123 · 测试门店',
  status: 'needs_review',
  message: '页面未能确认',
  updated_at: '2026-11-24T08:00:00Z',
}

const actions: WorkerAction[] = [
  {
    id: 2, run_id: 'a'.repeat(32), task_id: task.id, attempt_id: 'attempt-b', stage: 'capture', step: 2,
    action: 'click', started_at: '2026-11-24T08:00:02Z', elapsed_ms: 20, result: 'error', details: {},
  },
  {
    id: 1, run_id: 'a'.repeat(32), task_id: task.id, attempt_id: 'attempt-a', stage: 'launch', step: 1,
    action: 'discover', started_at: '2026-11-24T08:00:01Z', elapsed_ms: 10, result: 'ok', details: {},
  },
]

function renderDrawer(debugView: boolean) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <WorkerTaskDetailDrawer task={task} debugView={debugView} onOpenChange={() => undefined} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('WorkerTaskDetailDrawer', () => {
  it('keeps action diagnostics behind the debug-view gate', async () => {
    vi.mocked(fetchTaskArtifacts).mockResolvedValue({ files: [] })
    vi.mocked(fetchTaskActions).mockResolvedValue({ actions })
    renderDrawer(false)

    expect(screen.queryByText('动作时间线')).not.toBeInTheDocument()
    await waitFor(() => expect(fetchTaskArtifacts).toHaveBeenCalledWith(task.id, expect.any(AbortSignal), 'project-test'))
    expect(fetchTaskActions).not.toHaveBeenCalled()
  })

  it('loads debug actions through the task endpoint and displays them chronologically', async () => {
    vi.mocked(fetchTaskArtifacts).mockResolvedValue({ files: ['page-1.png', '../private.png'] })
    vi.mocked(fetchTaskActions).mockResolvedValue({ actions })
    renderDrawer(true)

    expect(await screen.findByText('动作时间线')).toBeInTheDocument()
    await waitFor(() => expect(fetchTaskActions).toHaveBeenCalledWith(task.id, null, expect.any(AbortSignal), 'project-test'))
    const list = screen.getByRole('list')
    const entries = Array.from(list.querySelectorAll('li'))
    expect(entries).toHaveLength(2)
    expect(entries[0]?.textContent).toContain('查找窗口')
    expect(entries[1]?.textContent).toContain('点击')
    expect(screen.getByAltText('page-1.png')).toBeInTheDocument()
    expect(screen.queryByAltText('../private.png')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: '下载调试包' })).toHaveAttribute('href', `/api/debug/runs/${'a'.repeat(32)}/export?project_id=project-test`)
  })
})
