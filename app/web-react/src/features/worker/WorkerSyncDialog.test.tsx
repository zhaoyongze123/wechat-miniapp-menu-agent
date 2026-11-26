import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { WorkerSyncDialog } from './WorkerSyncDialog'
import { fetchWorkerConnectionStatus } from './api'

vi.mock('../projects/ProjectProvider', () => ({ useProject: () => ({ projectId: 'project-worker' }) }))
vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, fetchWorkerConnectionStatus: vi.fn() }
})

describe('WorkerSyncDialog', () => {
  it('读取与所选项目对应的本机分配状态', async () => {
    vi.mocked(fetchWorkerConnectionStatus).mockResolvedValue({
      role: 'worker', paired: true, online: true,
      assignments: [{ id: 'assignment-1', task_id: 'task-1', status: 'pending_upload', store: '测试门店' }],
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <WorkerSyncDialog open onOpenChange={vi.fn()} />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('测试门店')).toBeInTheDocument()
    expect(fetchWorkerConnectionStatus).toHaveBeenCalledWith(expect.any(AbortSignal), 'project-worker')
  })
})
