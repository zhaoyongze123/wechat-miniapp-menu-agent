import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchAssignmentRecords, fetchResultFiles } from './api'
import { HubStoreDetailDrawer } from './HubStoreDetailDrawer'
import type { AssignmentRecord, HubTask } from './types'

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, fetchAssignmentRecords: vi.fn(), fetchResultFiles: vi.fn() }
})
vi.mock('../projects/ProjectProvider', () => ({ useProject: () => ({ projectId: 'project-test' }) }))

const task: HubTask = {
  id: '785c4a931d02b9768c12',
  source_name: 'E0005__LCA0000193429__佛山市__测试酒店__113.288719,22.954428',
  status: 'captured',
  store_state: 'captured',
  batch_name: '批次一',
  version: 'v2',
}

function record(overrides: Partial<AssignmentRecord>): AssignmentRecord {
  return {
    id: 'a'.repeat(32),
    task_id: task.id,
    worker_id: 'worker-1',
    worker_name: '采集电脑A',
    store: task.source_name,
    batch_name: '批次一',
    revision: 'v2',
    status: 'completed',
    disposition: '',
    result_message: '',
    ...overrides,
  }
}

function renderDrawer(overrides: Partial<HubTask> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <HubStoreDetailDrawer task={{ ...task, ...overrides }} onClose={() => undefined} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('HubStoreDetailDrawer', () => {
  it('按当前任务 ID 精确展示详情和二维码，不混入其他任务记录', async () => {
    vi.mocked(fetchAssignmentRecords).mockResolvedValue({
      records: [
        record({}),
        record({ id: 'b'.repeat(32), task_id: 'other-task', worker_name: '其他批次设备' }),
      ],
      total: 2,
      page: 1,
      pages: 1,
    })
    renderDrawer()

    const user = userEvent.setup()
    expect(screen.getByText('测试酒店')).toBeInTheDocument()
    expect(screen.getByText('原始门店名称')).toBeInTheDocument()
    expect(screen.getByText(task.id)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '查看二维码' }))
    expect(screen.getByAltText('任务二维码')).toHaveAttribute('src', `/api/tasks/${task.id}/source?project_id=project-test`)
    expect(await screen.findByText('采集电脑A')).toBeInTheDocument()
    expect(screen.queryByText('其他批次设备')).not.toBeInTheDocument()
    expect(fetchAssignmentRecords).toHaveBeenCalledWith(
      { workerId: '', phase: '', q: task.id, page: 1, size: 20 },
      expect.any(AbortSignal),
      'project-test',
    )
  })

  it('查看结果沿用只读入口，不提供回收动作', async () => {
    vi.mocked(fetchAssignmentRecords).mockResolvedValue({
      records: [record({ status: 'assigned' })],
      total: 1,
      page: 1,
      pages: 1,
    })
    vi.mocked(fetchResultFiles).mockResolvedValue({ files: [] })
    const user = userEvent.setup()
    renderDrawer()

    await user.click(await screen.findByRole('button', { name: '查看结果' }))

    expect(await screen.findByText('此入口仅用于查看分配和结果。')).toBeInTheDocument()
    expect(screen.getByText(/设备尚未上传结果/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '回收这条分配' })).not.toBeInTheDocument()
  })

  it('没有匹配分配时明确提示，不展示其他历史结果', async () => {
    vi.mocked(fetchAssignmentRecords).mockResolvedValue({ records: [], total: 0, page: 1, pages: 1 })
    renderDrawer({ status: 'pending', store_state: 'pending' })

    expect(await screen.findByText('当前任务暂无匹配的分配记录')).toBeInTheDocument()
  })

  it('支持在当前筛选结果内上一家/下一家', () => {
    vi.mocked(fetchAssignmentRecords).mockResolvedValue({ records: [], total: 0, page: 1, pages: 1 })
    const onPrev = vi.fn()
    const onNext = vi.fn()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <HubStoreDetailDrawer
          task={task}
          onClose={() => undefined}
          onPrev={onPrev}
          onNext={onNext}
          hasPrev
          hasNext
          position="2 / 5"
        />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(screen.getByText('2 / 5')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '上一家' }))
    fireEvent.click(screen.getByRole('button', { name: '下一家' }))
    expect(onPrev).toHaveBeenCalledTimes(1)
    expect(onNext).toHaveBeenCalledTimes(1)
  })
})
