import type { ReactNode } from 'react'
import * as RadixDialog from '@radix-ui/react-dialog'
import { Button } from '@openai/apps-sdk-ui/components/Button'

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
        <RadixDialog.Overlay className="fixed inset-0 z-40 bg-black/30" />
        <RadixDialog.Content
          className="fixed top-0 right-0 z-50 flex h-full w-[min(720px,96vw)] flex-col overflow-hidden border-l border-[var(--color-border-subtle)] bg-[var(--color-surface)] shadow-2xl"
          {...(description ? {} : { 'aria-describedby': undefined })}
        >
          <header className="flex items-start justify-between gap-4 border-b border-[var(--color-border-subtle)] px-5 py-4">
            <div className="min-w-0">
              <RadixDialog.Title className="truncate text-lg font-semibold text-[var(--color-text)]">{title}</RadixDialog.Title>
              {description ? (
                <RadixDialog.Description className="mt-1 text-xs text-[var(--color-text-secondary)]">
                  {description}
                </RadixDialog.Description>
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {headerActions}
              <RadixDialog.Close asChild>
                <Button color="secondary" variant="ghost" size="sm" aria-label="关闭详情">
                  关闭
                </Button>
              </RadixDialog.Close>
            </div>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
          {footer ? <footer className="border-t border-[var(--color-border-subtle)] px-5 py-3">{footer}</footer> : null}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  )
}
