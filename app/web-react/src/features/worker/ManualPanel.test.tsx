import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ManualPanel } from './ManualPanel'
import type { ManualState } from './types'

function makeManual(overrides: Partial<ManualState> = {}): ManualState {
  return {
    run_id: 'run-1',
    wait_id: 'wait-1',
    task_id: 'task-1',
    store_name: 'E02636_老乡鸡_上海',
    observed_store_name: '',
    index: 2,
    total: 10,
    stage: 'waiting_human',
    waiting: true,
    decision: '',
    reason: 'not_ordering',
    ...overrides,
  }
}

describe('ManualPanel', () => {
  it('支持 AI 识别回填实际门店名称，且可手动编辑即时保存', async () => {
    const onCapture = vi.fn()
    const onReject = vi.fn()
    const onRecognizeStoreName = vi.fn().mockResolvedValue({
      ok: true,
      task_id: 'task-1',
      observed_store_name: '老乡鸡(临港万达店)',
      recognized_name: '老乡鸡(临港万达店)',
      reason: '顶部标题栏清晰可见',
      stale: false,
    })
    const onSaveStoreName = vi.fn().mockResolvedValue(undefined)

    render(
      <ManualPanel
        manual={makeManual()}
        busy={false}
        error=""
        onCapture={onCapture}
        onReject={onReject}
        onRecognizeStoreName={onRecognizeStoreName}
        onSaveStoreName={onSaveStoreName}
      />,
    )

    expect(screen.getByText('半自动采集 · 2 / 10')).toBeInTheDocument()
    expect(screen.getByText('E02636_老乡鸡_上海')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /AI 识别/ }))
    await waitFor(() => {
      expect(screen.getByLabelText('实际门店名称')).toHaveValue('老乡鸡(临港万达店)')
    })
    expect(onRecognizeStoreName).toHaveBeenCalledTimes(1)

    const input = screen.getByLabelText('实际门店名称')
    fireEvent.change(input, { target: { value: '老乡鸡(上海临港万达广场店)' } })
    fireEvent.blur(input)

    await waitFor(() => {
      expect(onSaveStoreName).toHaveBeenCalledWith('老乡鸡(上海临港万达广场店)')
    })
  })

  it('AI 识别失败时展示提示且不阻塞开始截图', async () => {
    const onCapture = vi.fn()
    const onRecognizeStoreName = vi.fn().mockRejectedValue(new Error('AI 识别超时'))

    render(
      <ManualPanel
        manual={makeManual()}
        busy={false}
        error=""
        onCapture={onCapture}
        onReject={vi.fn()}
        onRecognizeStoreName={onRecognizeStoreName}
        onSaveStoreName={vi.fn().mockResolvedValue(undefined)}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /AI 识别/ }))
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('AI 识别超时')
    })

    fireEvent.click(screen.getByRole('button', { name: /开始截图/ }))
    expect(onCapture).toHaveBeenCalledTimes(1)
  })

  it('低频设置默认收纳，点击设置按钮后展开', () => {
    render(
      <ManualPanel
        manual={makeManual()}
        busy={false}
        error=""
        onCapture={vi.fn()}
        onReject={vi.fn()}
        extraSettings={<div>测试提示音区</div>}
      />,
    )

    expect(screen.queryByText('测试提示音区')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '辅助设置' }))
    expect(screen.getByText('测试提示音区')).toBeInTheDocument()
  })
})
