import { useState } from 'react'
import * as RadixDialog from '@radix-ui/react-dialog'

interface ImagePreviewProps {
  open: boolean
  src: string
  alt: string
  caption?: string
  onOpenChange: (open: boolean) => void
}

/**
 * 证据图片预览：默认等比缩放到可视区域，可切换原始尺寸后滚动查看。
 * 图片本身来自同源受控路由，关闭时只卸载节点，不缓存客户端副本。
 */
export function ImagePreview({ open, src, alt, caption, onOpenChange }: ImagePreviewProps) {
  const [actualSize, setActualSize] = useState(false)
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-50 bg-black/85" />
        <RadixDialog.Content
          className="fixed inset-3 z-[60] flex flex-col overflow-hidden rounded-xl border border-shell-line bg-shell-900"
          aria-describedby={undefined}
        >
          <header className="flex items-center justify-between gap-4 border-b border-shell-line px-4 py-2.5">
            <RadixDialog.Title className="truncate text-sm text-shell-text">{caption ?? alt}</RadixDialog.Title>
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="rounded-md border border-shell-line px-2 py-1 text-xs text-shell-muted hover:border-accent-400 hover:text-shell-text"
                aria-pressed={actualSize}
                onClick={() => setActualSize((value) => !value)}
              >
                {actualSize ? '适应窗口' : '原始尺寸'}
              </button>
              <RadixDialog.Close asChild>
                <button
                  type="button"
                  className="rounded-md border border-shell-line px-2 py-1 text-xs text-shell-muted hover:border-accent-400 hover:text-shell-text"
                >
                  关闭
                </button>
              </RadixDialog.Close>
            </div>
          </header>
          <div className="min-h-0 flex-1 overflow-auto bg-black/40 p-3">
            <img
              src={src}
              alt={alt}
              className={actualSize ? 'max-w-none' : 'mx-auto max-h-full max-w-full object-contain'}
            />
          </div>
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  )
}
