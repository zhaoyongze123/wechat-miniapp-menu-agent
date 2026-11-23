import { useState } from 'react'
import * as RadixDialog from '@radix-ui/react-dialog'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Image } from '@openai/apps-sdk-ui/components/Image'

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
        <RadixDialog.Overlay className="fixed inset-0 z-50 bg-black/70" />
        <RadixDialog.Content
          className="fixed inset-3 z-[60] flex flex-col overflow-hidden rounded-2xl border border-[var(--color-border-subtle)] bg-[var(--color-surface)]"
          aria-describedby={undefined}
        >
          <header className="flex items-center justify-between gap-4 border-b border-[var(--color-border-subtle)] px-4 py-2.5">
            <RadixDialog.Title className="truncate text-sm text-[var(--color-text)]">{caption ?? alt}</RadixDialog.Title>
            <div className="flex items-center gap-2">
              <Button
                color="secondary"
                variant="outline"
                size="sm"
                aria-pressed={actualSize}
                onClick={() => setActualSize((value) => !value)}
              >
                {actualSize ? '适应窗口' : '原始尺寸'}
              </Button>
              <RadixDialog.Close asChild>
                <Button color="secondary" variant="outline" size="sm">
                  关闭
                </Button>
              </RadixDialog.Close>
            </div>
          </header>
          <div className="min-h-0 flex-1 overflow-auto bg-[var(--color-surface-secondary)] p-3">
            <Image
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
