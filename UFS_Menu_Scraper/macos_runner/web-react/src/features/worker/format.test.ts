import { describe, expect, it } from 'vitest'
import {
  canResume,
  canSwitchToManual,
  captureModeLabel,
  filterTasks,
  manualStageLabel,
  pageTasks,
  progressOf,
  reasonLabel,
  runStatusOf,
  sortTasks,
  stateKeyOf,
  stateMetaOf,
} from './format'
import type { WorkerRun, WorkerTask } from './types'

function task(overrides: Partial<WorkerTask>): WorkerTask {
  return { id: 't', source_name: 'SH001__上海市__示例门店__31.1,121.1', status: 'pending', ...overrides }
}

describe('worker/format', () => {
  it('映射运行状态与未知状态', () => {
    expect(runStatusOf('interrupted')).toEqual({ label: '已中断', tone: 'danger' })
    expect(runStatusOf(undefined).label).toBe('未开始')
    expect(runStatusOf('mystery').label).toBe('mystery')
    expect(captureModeLabel('manual')).toBe('半自动人工')
  })

  it('门店状态以 store_state 为唯一事实源，缺失时回退', () => {
    expect(stateKeyOf(task({ store_state: 'returned' }))).toBe('returned')
    expect(stateKeyOf(task({ status: 'captured' }))).toBe('captured')
    expect(stateKeyOf(task({ status: 'error' }))).toBe('review')
    expect(stateKeyOf(task({ reject_reason: 'blur' }))).toBe('rejected')
    expect(stateMetaOf(task({ store_state: 'review' })).label).toBe('待检查')
  })

  it('按状态、批次与关键词过滤', () => {
    const rows = [
      task({ id: 'a', batch_id: 'b1', batch_name: '第一批', source_name: 'SH001__上海市__甲店__31.1,121.1' }),
      task({ id: 'b', batch_id: 'b2', store_state: 'rejected', source_name: 'SH002__杭州市__乙店__31.2,121.2' }),
    ]
    expect(filterTasks(rows, { filter: 'all', q: '', batch: '' })).toHaveLength(2)
    expect(filterTasks(rows, { filter: 'rejected', q: '', batch: '' }).map((row) => row.id)).toEqual(['b'])
    expect(filterTasks(rows, { filter: 'all', q: '乙店', batch: '' }).map((row) => row.id)).toEqual(['b'])
    expect(filterTasks(rows, { filter: 'all', q: '', batch: 'b1' }).map((row) => row.id)).toEqual(['a'])
  })

  it('排序与分页保持稳定', () => {
    const rows = [
      task({ id: 'a', source_name: 'SH002__b', updated_at: '2026-11-24T08:00:00Z' }),
      task({ id: 'b', source_name: 'SH001__a', updated_at: '2026-11-24T09:00:00Z' }),
    ]
    expect(sortTasks(rows, 'source').map((row) => row.id)).toEqual(['b', 'a'])
    expect(sortTasks(rows, 'recent').map((row) => row.id)).toEqual(['b', 'a'])
    const paged = pageTasks([1, 2, 3, 4, 5].map((index) => task({ id: String(index) })), 2, 2)
    expect(paged.rows.map((row) => row.id)).toEqual(['3', '4'])
    expect(paged.pages).toBe(3)
    expect(pageTasks([], 3, 20).page).toBe(1)
  })

  it('进度不越过总数，剩余不为负', () => {
    const run = { total: 10, processed: 12, remaining: -2 } as WorkerRun
    expect(progressOf(run)).toEqual({ total: 10, processed: 10, percent: 100, remaining: 0 })
    expect(progressOf(null)).toEqual({ total: 0, processed: 0, percent: 0, remaining: 0 })
  })

  it('仅暂停/阻塞/中断可继续，半自动切换只对 Windows 全自动暂停批次开放', () => {
    expect(canResume({ status: 'paused' } as WorkerRun)).toBe(true)
    expect(canResume({ status: 'terminated' } as WorkerRun)).toBe(false)
    expect(canSwitchToManual({ status: 'paused', capture_mode: 'auto' } as WorkerRun)).toBe(true)
    expect(canSwitchToManual({ status: 'paused', capture_mode: 'manual' } as WorkerRun)).toBe(false)
  })

  it('原因与人工阶段有可读文案', () => {
    expect(reasonLabel('blur')).toBe('图片模糊 / 无法识别')
    expect(reasonLabel('unknown')).toBe('未记录原因')
    expect(manualStageLabel({ stage: 'waiting', waiting: true } as never)).toBe('等待人工处理')
    expect(manualStageLabel(null)).toBe('')
  })
})
