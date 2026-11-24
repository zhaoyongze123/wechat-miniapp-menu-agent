import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { BatchCell } from './TaskCenterPage'
import type { BatchSummary, HistoryRecord } from './types'

function batch(index: number, versions: string[] = ['v1', 'v2']): BatchSummary {
  return { id: `batch-${index}`, name: `批次${index}`, versions, stores: 1 }
}

function record(overrides: Partial<HistoryRecord>): HistoryRecord {
  return { kind: 'capture', id: 'run-1', status: 'completed', ...overrides }
}

describe('BatchCell', () => {
  it('多批次只展示紧凑摘要，完整范围保留在标题和详情中', () => {
    const batches = Array.from({ length: 12 }, (_, index) => batch(index + 1))
    const { container } = render(<BatchCell record={record({ batches })} />)

    expect(screen.getByText('涉及 12 个批次')).toBeInTheDocument()
    expect(screen.getByText('批次1 · v1 等 12 个')).toBeInTheDocument()
    expect(screen.queryByText('批次12 · v1 · v2')).not.toBeInTheDocument()
    const root = container.firstElementChild as HTMLElement
    expect(root.getAttribute('title')).toContain('批次12 · v1 · v2')
    expect(root.className).toContain('max-w-40')
    expect(container.querySelectorAll('span')).toHaveLength(2)
  })

  it('单批次展示名称与全部版本', () => {
    render(<BatchCell record={record({ batches: [batch(1, ['v1', 'v2', 'v3'])] })} />)

    expect(screen.getByText('批次1')).toBeInTheDocument()
    expect(screen.getByText('v1 / v2 / v3')).toBeInTheDocument()
  })

  it('缺少批次明细时回退到批次名或占位符', () => {
    const { rerender } = render(<BatchCell record={record({ batch_name: '历史批次' })} />)
    expect(screen.getByText('历史批次')).toBeInTheDocument()

    rerender(<BatchCell record={record({})} />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})
