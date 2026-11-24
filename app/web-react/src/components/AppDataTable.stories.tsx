import type { Meta, StoryObj } from '@storybook/react-vite'
import { useState } from 'react'
import { expect, fn, userEvent, within } from 'storybook/test'
import { AppDataTable, type DataColumn } from './AppDataTable'
import { CellText } from './CellText'
import { StatusBadge } from './StatusBadge'

interface StoreRow {
  id: string
  name: string
  batch: string
  status: 'ready' | 'pending' | 'failed'
  message: string
}

const rows: StoreRow[] = [
  {
    id: 'store-1',
    name: '示例门店（望京 SOHO 旗舰店·三层美食广场东区）',
    batch: '2026-11-24 首次采集批次',
    status: 'ready',
    message: '已生成菜单结果，等待发布。',
  },
  {
    id: 'store-2',
    name: '示例门店 B',
    batch: '2026-11-24 首次采集批次',
    status: 'pending',
    message: '等待人工确认菜品分类。',
  },
  {
    id: 'store-3',
    name: '示例门店 C',
    batch: '2026-11-24 返工批次',
    status: 'failed',
    message: '截图读取失败：小程序返回空白页面，请重新采集后再试。',
  },
]

const columns: DataColumn<StoreRow>[] = [
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
    width: '16rem',
    cell: (row) => (
      <CellText copyable copyLabel="门店名称">
        {row.name}
      </CellText>
    ),
  },
  {
    id: 'batch',
    header: '批次',
    width: '14rem',
    cell: (row) => <CellText>{row.batch}</CellText>,
  },
  {
    id: 'status',
    header: '状态',
    width: '7rem',
    cell: (row) => (
      <StatusBadge tone={row.status === 'ready' ? 'success' : row.status === 'failed' ? 'danger' : 'warning'}>
        {row.status === 'ready' ? '可发布' : row.status === 'failed' ? '失败' : '待确认'}
      </StatusBadge>
    ),
  },
  {
    id: 'message',
    header: '说明',
    width: '16rem',
    cell: (row) => <CellText>{row.message}</CellText>,
  },
  {
    id: 'actions',
    header: '操作',
    width: '6rem',
    sticky: 'right',
    cell: () => (
      <button type="button" className="text-sm underline underline-offset-2">
        详情
      </button>
    ),
  },
]

const meta: Meta = {
  title: '组件/数据表',
  parameters: { layout: 'padded' },
}

export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  render: () => (
    <AppDataTable caption="示例门店列表" columns={columns} rows={rows} rowKey={(row) => row.id} />
  ),
}

export const Loading: Story = {
  render: () => (
    <AppDataTable caption="示例门店列表" columns={columns} rows={rows} rowKey={(row) => row.id} loading />
  ),
}

export const Empty: Story = {
  render: () => <AppDataTable caption="示例门店列表" columns={columns} rows={[]} rowKey={(row) => row.id} />,
}

export const NarrowStickyColumns: Story = {
  render: () => (
    <div className="w-[24rem]">
      <AppDataTable
        caption="窄容器下的固定列"
        columns={columns}
        rows={rows}
        rowKey={(row) => row.id}
        minWidthClass="min-w-[64rem]"
      />
    </div>
  ),
}

const rowActivate = fn()

export const KeyboardRowActivation: Story = {
  render: () => (
    <AppDataTable
      caption="键盘进入详情"
      columns={columns}
      rows={rows}
      rowKey={(row) => row.id}
      onRowActivate={rowActivate}
    />
  ),
  play: async ({ canvasElement }) => {
    rowActivate.mockClear()
    const canvas = within(canvasElement)
    const row = canvas.getAllByRole('row')[1]
    if (!row) throw new Error('缺少示例数据行')
    row.focus()
    await userEvent.keyboard('{Enter}')
    await expect(rowActivate).toHaveBeenCalledTimes(1)
  },
}

/** 行内展开：主行保留列表语义，详情在下一行跨列展示。 */
function ExpandableTable() {
  const [expanded, setExpanded] = useState<string | null>(null)
  const detailColumns: DataColumn<StoreRow>[] = [
    ...columns.filter((column) => column.id !== 'actions'),
    {
      id: 'toggle',
      header: '详情',
      width: '5rem',
      cell: (row) => (
        <button
          type="button"
          className="text-sm underline underline-offset-2"
          aria-expanded={expanded === row.id}
          onClick={() => setExpanded(expanded === row.id ? null : row.id)}
        >
          {expanded === row.id ? '收起' : '展开'}
        </button>
      ),
    },
  ]
  return (
    <AppDataTable
      caption="可展开明细的门店列表"
      columns={detailColumns}
      rows={rows}
      rowKey={(row) => row.id}
      subRow={(row) => (expanded === row.id ? <p className="text-sm text-default">失败说明：{row.message}</p> : null)}
    />
  )
}

export const ExpandableRow: Story = {
  render: () => <ExpandableTable />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getAllByRole('button', { name: '展开' })[0]!)
    await expect(canvas.getByText(/失败说明：/)).toBeVisible()
    await userEvent.click(canvas.getByRole('button', { name: '收起' }))
    await expect(canvas.queryByText(/失败说明：/)).toBeNull()
  },
}
