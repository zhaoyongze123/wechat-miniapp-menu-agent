import type { Meta, StoryObj } from '@storybook/react-vite'
import { useState } from 'react'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { AppDialog } from './AppDialog'
import { AppDrawer } from './AppDrawer'
import { ConfirmActionDialog } from './ConfirmActionDialog'
import { ToastProvider, useToast } from './ToastProvider'

function ToastDemo() {
  const toast = useToast()
  return (
    <div className="flex flex-wrap gap-2">
      <Button color="secondary" variant="outline" size="sm" onClick={() => toast.push('配置已保存', 'success')}>
        成功提示
      </Button>
      <Button color="secondary" variant="outline" size="sm" onClick={() => toast.push('已同步 3 个结果，2 个等待补传', 'info')}>
        普通提示
      </Button>
      <Button color="secondary" variant="outline" size="sm" onClick={() => toast.push('共享后台连接中断，请检查网络后重试', 'danger')}>
        失败提示
      </Button>
      <Button color="secondary" variant="ghost" size="sm" onClick={() => toast.push('配置已保存', 'success')}>
        重复触发（自动去重）
      </Button>
    </div>
  )
}

function DialogDemo() {
  const [open, setOpen] = useState(true)
  return (
    <>
      <Button color="secondary" variant="outline" size="sm" onClick={() => setOpen(true)}>
        打开示例弹窗
      </Button>
      <AppDialog
        open={open}
        onOpenChange={setOpen}
        title="这是一个用于验证长标题省略与关闭按钮位置的示例弹窗标题"
        description="示例数据，不连接任何生产接口。"
        footer={
          <div className="flex justify-end gap-2">
            <Button color="secondary" variant="outline" size="sm" onClick={() => setOpen(false)}>
              取消
            </Button>
            <Button color="primary" size="sm" onClick={() => setOpen(false)}>
              确认
            </Button>
          </div>
        }
      >
        <p className="text-sm leading-relaxed text-default">
          弹窗头部与底部固定，内容区域独立滚动。长内容不会把关闭按钮或主要操作顶出视口。
        </p>
      </AppDialog>
    </>
  )
}

function DrawerDemo() {
  const [open, setOpen] = useState(true)
  return (
    <>
      <Button color="secondary" variant="outline" size="sm" onClick={() => setOpen(true)}>
        打开示例抽屉
      </Button>
      <AppDrawer
        open={open}
        onOpenChange={setOpen}
        title="示例门店详情（长名称用于验证省略与完整查看）"
        description="只读详情；关闭后焦点回到触发按钮。"
        footer={
          <div className="flex justify-end">
            <Button color="secondary" variant="outline" size="sm" onClick={() => setOpen(false)}>
              关闭
            </Button>
          </div>
        }
      >
        <div className="space-y-3">
          {Array.from({ length: 24 }, (_, index) => (
            <p key={index} className="text-sm leading-relaxed text-default">
              第 {index + 1} 段示例证据说明：内容区域独立滚动，头部保持可见。
            </p>
          ))}
        </div>
      </AppDrawer>
    </>
  )
}

const meta: Meta = {
  title: '组件/反馈与弹层',
  parameters: { layout: 'padded' },
}

export default meta
type Story = StoryObj<typeof meta>

export const Toasts: Story = {
  render: () => (
    <ToastProvider>
      <ToastDemo />
    </ToastProvider>
  ),
}

export const Dialog: Story = {
  render: () => <DialogDemo />,
}

export const Drawer: Story = {
  render: () => <DrawerDemo />,
}

export const ConfirmBusy: Story = {
  render: () => (
    <ConfirmActionDialog
      open
      title="确认清理调试日志"
      description={
        <div className="space-y-1">
          <p>将删除已保存的调试日志和调试截图。</p>
          <p>门店素材、菜单截图与采集结果会保留。</p>
        </div>
      }
      confirmLabel="确认清理"
      tone="danger"
      busy
      onConfirm={() => undefined}
      onOpenChange={() => undefined}
    />
  ),
}
