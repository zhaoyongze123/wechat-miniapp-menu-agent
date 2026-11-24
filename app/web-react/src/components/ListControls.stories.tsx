import type { Meta, StoryObj } from '@storybook/react-vite'
import { useState } from 'react'
import { expect, userEvent, within } from 'storybook/test'
import { BulkActionBar } from './BulkActionBar'
import { FilterToolbar } from './FilterToolbar'
import { StatCard } from './StatCard'
import { StatusBadge } from './StatusBadge'
import { SearchInput } from './SearchInput'
import { CellText } from './CellText'
import { KeyValueList, KeyValuePair } from './KeyValueList'

const meta: Meta = {
  title: '组件/列表控制与信息展示',
  parameters: { layout: 'padded' },
}

export default meta
type Story = StoryObj<typeof meta>

function FilterDemo() {
  const [search, setSearch] = useState('望京')
  const [selected, setSelected] = useState(2)
  return (
    <div className="space-y-3">
      <FilterToolbar
        label="示例筛选"
        activeCount={search ? 1 : 0}
        onClear={() => setSearch('')}
        summary="共 12 家门店"
      >
        <SearchInput className="w-64" label="门店" placeholder="搜索门店名称" value={search} onChange={setSearch} />
      </FilterToolbar>
      <BulkActionBar count={selected} hint="仅当前页可批量选择" onClear={() => setSelected(0)}>
        <button type="button" className="text-sm underline underline-offset-2" onClick={() => setSelected(1)}>
          示例操作
        </button>
      </BulkActionBar>
    </div>
  )
}

export const FilterAndBulk: Story = {
  render: () => <FilterDemo />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: '清除门店' }))
    await expect(canvas.getByRole('searchbox', { name: '门店' })).toHaveValue('')
    await userEvent.click(canvas.getByRole('button', { name: '清空选择' }))
    await expect(canvas.queryByRole('region', { name: '批量操作' })).toBeNull()
  },
}

export const MetricsAndStatus: Story = {
  render: () => (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard label="已配对" value={8} />
        <StatCard label="待更新" value={3} tone="warning" />
        <StatCard label="失败" value={1} tone="danger" />
        <StatCard label="已确认" value={42} tone="success" />
      </div>
      <div className="flex flex-wrap gap-2">
        <StatusBadge tone="success">已完成</StatusBadge>
        <StatusBadge tone="warning">等待中</StatusBadge>
        <StatusBadge tone="danger">失败</StatusBadge>
        <StatusBadge tone="secondary">历史</StatusBadge>
      </div>
      <CellText copyable copyLabel="任务 ID">task-0123456789abcdef0123456789abcdef</CellText>
    </div>
  ),
}

export const DetailFields: Story = {
  render: () => (
    <div className="space-y-4">
      <dl className="grid gap-3 sm:grid-cols-2">
        <KeyValuePair label="门店">示例门店（望京 SOHO）</KeyValuePair>
        <KeyValuePair label="调用次数">0</KeyValuePair>
        <KeyValuePair label="失败原因">{''}</KeyValuePair>
        <KeyValuePair label="执行设备">{null}</KeyValuePair>
      </dl>
      <KeyValueList
        items={[
          { label: '任务 ID', value: 'task-0123456789abcdef0123456789abcdef', copy: 'task-0123456789abcdef0123456789abcdef', mono: true },
          { label: '批次', value: '2026-11-24 首次采集批次' },
          { label: '说明', value: '等待人工确认菜品分类。' },
        ]}
      />
    </div>
  ),
}
