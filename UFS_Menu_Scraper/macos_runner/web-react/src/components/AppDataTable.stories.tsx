import type { Meta, StoryObj } from '@storybook/react-vite'
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
