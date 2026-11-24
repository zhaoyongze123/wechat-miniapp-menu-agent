import { describe, expect, it } from 'vitest'
import { filterCaptureTasks, taskDetailIsProgressing } from './TaskDetailDrawer'
import type { CaptureTaskEntry } from './types'

const tasks: CaptureTaskEntry[] = [
  { task_id: 'E00459', store_name: '上海虾乐园', label: '已截图待复核', status: 'captured' },
  { task_id: 'E3022', store_name: '成都山城外婆', label: '待检查', status: 'review' },
  { task_id: 'E2829', store_name: '北京火灶潮汕砂锅粥', label: '不合格', status: 'rejected' },
  { task_id: 'ZA007', store_name: '原始目录名', source_name: '原始目录名__上海市__门店', label: '未记录结果', status: '' },
]

describe('运行任务详情刷新状态', () => {
  it('运行、排队和状态切换期间保持轮询，暂停和终态停止', () => {
    for (const status of ['queued', 'running', 'pause_requested', 'resuming', 'terminating']) {
      expect(taskDetailIsProgressing(status)).toBe(true)
    }
    for (const status of ['paused', 'completed', 'failed', 'terminated', undefined]) {
      expect(taskDetailIsProgressing(status)).toBe(false)
    }
  })
})

describe('逐店结果搜索与筛选', () => {
  it('按门店编号、名称和原始来源查找完整记录', () => {
    expect(filterCaptureTasks(tasks, 'e3022', 'all').map((task) => task.store_name)).toEqual(['成都山城外婆'])
    expect(filterCaptureTasks(tasks, '火灶潮汕', 'all').map((task) => task.task_id)).toEqual(['E2829'])
    expect(filterCaptureTasks(tasks, '原始目录名__上海市', 'all').map((task) => task.task_id)).toEqual(['ZA007'])
  })

  it('按业务结果分桶，并把旧记录缺项留在待处理或未记录分类', () => {
    expect(filterCaptureTasks(tasks, '', 'captured').map((task) => task.task_id)).toEqual(['E00459'])
    expect(filterCaptureTasks(tasks, '', 'review').map((task) => task.task_id)).toEqual(['E3022'])
    expect(filterCaptureTasks(tasks, '', 'rejected').map((task) => task.task_id)).toEqual(['E2829'])
    expect(filterCaptureTasks(tasks, '', 'pending').map((task) => task.task_id)).toEqual(['ZA007'])
  })
})
