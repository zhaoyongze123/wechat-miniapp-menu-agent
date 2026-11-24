import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { AppDataTable, type DataColumn } from './AppDataTable'

interface Row {
  id: string
  name: string
}

const rows: Row[] = [
  { id: '1', name: '测试门店' },
  { id: '2', name: '另一家门店' },
]

function columns(onOpen = vi.fn()): DataColumn<Row>[] {
  return [
    {
      id: 'select',
      header: '选择',
      width: '3.25rem',
      sticky: 'left',
      cell: (row) => <input type="checkbox" aria-label={`选择 ${row.name}`} />,
    },
    {
      id: 'name',
      header: '门店',
      width: '12rem',
      cell: (row) => <span className="block truncate">{row.name}</span>,
    },
    {
      id: 'actions',
      header: '操作',
      width: '5.5rem',
      sticky: 'right',
      cell: (row) => (
        <button type="button" onClick={() => onOpen(row.id)}>
          详情
        </button>
      ),
    },
  ]
}

describe('AppDataTable', () => {
  it('配置列宽时使用固定布局并输出 colgroup', () => {
    const { container } = render(
      <AppDataTable caption="测试列表" columns={columns()} rows={rows} rowKey={(row) => row.id} />,
    )

    const table = container.querySelector('table')
    expect(table?.className).toContain('table-fixed')
    const cols = container.querySelectorAll('col')
    expect(cols).toHaveLength(3)
    expect((cols[0] as HTMLElement).style.width).toBe('3.25rem')
    expect((cols[2] as HTMLElement).style.width).toBe('5.5rem')
  })

  it('左右固定列同时保留表头吸顶层', () => {
    const { container } = render(
      <AppDataTable caption="测试列表" columns={columns()} rows={rows} rowKey={(row) => row.id} />,
    )

    const heads = container.querySelectorAll('th')
    expect(heads[0]?.className).toContain('sticky')
    expect(heads[0]?.className).toContain('left-0')
    expect(heads[2]?.className).toContain('sticky')
    expect(heads[2]?.className).toContain('right-0')
    expect(container.querySelector('thead')?.className).toContain('sticky')

    const firstRowCells = container.querySelectorAll('tbody tr')[0]?.querySelectorAll('td') ?? []
    expect(firstRowCells[0]?.className).toContain('left-0')
    expect(firstRowCells[2]?.className).toContain('right-0')
  })

  it('未配置列宽时保持自动布局，避免裁切操作按钮', () => {
    const autoColumns = columns().map(({ width: _width, ...column }) => column)
    const { container } = render(
      <AppDataTable caption="测试列表" columns={autoColumns} rows={rows} rowKey={(row) => row.id} />,
    )

    expect(container.querySelector('table')?.className).not.toContain('table-fixed')
    expect(container.querySelectorAll('col')).toHaveLength(0)
  })
})
