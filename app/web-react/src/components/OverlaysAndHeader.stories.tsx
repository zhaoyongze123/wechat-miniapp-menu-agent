import type { Meta, StoryObj } from '@storybook/react-vite'
import { useState } from 'react'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { expect, userEvent, within } from 'storybook/test'
import { AppDialog } from './AppDialog'
import { AppDrawer } from './AppDrawer'
import { ConfirmActionDialog } from './ConfirmActionDialog'
import { PageHeader } from './PageHeader'

const meta: Meta = {
  title: '组件/弹层与页头',
  parameters: { layout: 'padded' },
}

export default meta
type Story = StoryObj<typeof meta>

export const Header: Story = {
  render: () => (
    <PageHeader
      title="质量审核"
      description="处理待检查门店与不合格记录，处置结果直接进入对应业务状态。"
      actions={<Button color="secondary" variant="outline" size="sm">刷新</Button>}
    />
  ),
}

function OverlayDemo() {
  const [dialogOpen, setDialogOpen] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  return (
    <div className="flex flex-wrap gap-2">
      <Button color="primary" size="sm" onClick={() => setDialogOpen(true)}>
        打开弹窗
      </Button>
      <Button color="primary" size="sm" onClick={() => setDrawerOpen(true)}>
        打开展示抽屉
      </Button>
      <Button size="sm" color="danger" onClick={() => setConfirmOpen(true)}>
        打开确认
      </Button>
      <AppDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        title="门店详情"
        description="展示当前任务与最近一次采集结果。"
        footer={<span className="text-xs text-secondary">底部操作区固定，内容独立滚动。</span>}
      >
        <p className="text-sm text-default">弹窗内容区域。</p>
      </AppDialog>
      <AppDrawer
        open={drawerOpen}
        onOpenChange={setDrawerOpen}
        title="待检查门店"
        description="当前结果 · 批次 A"
        footer={<span className="text-xs text-secondary">底部操作区固定。</span>}
      >
        <p className="text-sm text-default">抽屉内容区域。</p>
      </AppDrawer>
      <ConfirmActionDialog
        open={confirmOpen}
        title="确认回收分配"
        description={<p>将回收 2 条分配，涉及 采集电脑 A。回收后门店立即解除占用。</p>}
        confirmLabel="确认回收"
        tone="danger"
        onConfirm={() => setConfirmOpen(false)}
        onOpenChange={setConfirmOpen}
      />
    </div>
  )
}

export const Overlays: Story = {
  render: () => <OverlayDemo />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: '打开弹窗' }))
    const dialog = await canvas.findByRole('dialog')
    await expect(within(dialog).getByText('门店详情')).toBeVisible()
    await userEvent.click(within(dialog).getByRole('button', { name: '关闭' }))
    await expect(canvas.queryByRole('dialog')).toBeNull()
  },
}
