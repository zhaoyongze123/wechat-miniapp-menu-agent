import type { ReactNode } from 'react'
import * as RadixDialog from '@radix-ui/react-dialog'
import { Button } from '@openai/apps-sdk-ui/components/Button'

interface AppDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: string
  children: ReactNode
  footer?: ReactNode
  /** 标题右侧的自定义操作（如重命名入口）。 */
  headerActions?: ReactNode
  widthClass?: string
}

/** 居中弹窗：Escape、遮罩点击与关闭按钮均关闭，焦点由 Radix 圈定并回焦。 */
export function AppDialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  headerActions,
  widthClass = 'w-[min(760px,94vw)]',
}: AppDialogProps) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-40 bg-black/30" />
        <RadixDialog.Content
          className={`fixed top-1/2 left-1/2 z-50 flex max-h-[88vh] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-2xl border border-line-subtle bg-panel shadow-2xl ${widthClass}`}
          {...(description ? {} : { 'aria-describedby': undefined })}
        >
          <header className="flex items-start justify-between gap-4 border-b border-line-subtle px-5 py-4">
            <div className="min-w-0">
              <RadixDialog.Title className="text-[17px] font-semibold text-ink">{title}</RadixDialog.Title>
              {description ? (
                <RadixDialog.Description className="mt-1 text-xs text-ink-muted">
                  {description}
                </RadixDialog.Description>
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {headerActions}
              <RadixDialog.Close asChild>
                <Button color="secondary" variant="ghost" size="sm" aria-label="关闭">
                  关闭
                </Button>
              </RadixDialog.Close>
            </div>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
          {footer ? <footer className="border-t border-line-subtle px-5 py-3">{footer}</footer> : null}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  )
}
