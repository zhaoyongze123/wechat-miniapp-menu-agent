import type { ReactNode } from 'react'
import * as RadixDialog from '@radix-ui/react-dialog'

interface AppDrawerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: string
  children: ReactNode
  footer?: ReactNode
  headerActions?: ReactNode
}

/**
 * 右侧抽屉：头部与脚部固定，内容独立滚动，避免长证据把关闭按钮顶出视口。
 * 关闭时由 Radix 把焦点还给触发元素。
 */
export function AppDrawer({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  headerActions,
}: AppDrawerProps) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-40 bg-black/60 backdrop-blur-[1px]" />
        <RadixDialog.Content
          className="fixed right-0 top-0 z-50 flex h-full w-[min(720px,96vw)] flex-col overflow-hidden border-l border-shell-line bg-shell-900 shadow-2xl"
          {...(description ? {} : { 'aria-describedby': undefined })}
        >
          <header className="flex items-start justify-between gap-4 border-b border-shell-line px-5 py-4">
            <div className="min-w-0">
              <RadixDialog.Title className="truncate text-base font-semibold text-shell-text">{title}</RadixDialog.Title>
              {description ? (
                <RadixDialog.Description className="mt-1 text-xs text-shell-muted">
                  {description}
                </RadixDialog.Description>
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {headerActions}
              <RadixDialog.Close asChild>
                <button
                  type="button"
                  className="rounded-md border border-shell-line px-2 py-1 text-xs text-shell-muted hover:border-accent-400 hover:text-shell-text"
                  aria-label="关闭详情"
                >
                  关闭
                </button>
              </RadixDialog.Close>
            </div>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
          {footer ? <footer className="border-t border-shell-line px-5 py-3">{footer}</footer> : null}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  )
}
