import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ImagePreview } from './ImagePreview'

describe('ImagePreview', () => {
  it('支持多图切换、键盘缩放和只读旋转预览', () => {
    render(
      <ImagePreview
        open
        src="/first.png"
        alt="第一张"
        items={[{ src: '/first.png', alt: '第一张' }, { src: '/second.png', alt: '第二张' }]}
        onOpenChange={vi.fn()}
      />,
    )

    expect(screen.getByRole('img', { name: '第一张' })).toHaveAttribute('src', '/first.png')
    fireEvent.click(screen.getByRole('button', { name: '下一张' }))
    expect(screen.getByRole('img', { name: '第二张' })).toHaveAttribute('src', '/second.png')
    fireEvent.keyDown(screen.getByRole('dialog'), { key: '+' })
    expect(screen.getByText('2 / 2 · 120%')).toBeInTheDocument()
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'r' })
    expect(screen.getByRole('img', { name: '第二张' })).toHaveStyle({ transform: 'translate(0px, 0px) scale(1.2) rotate(90deg)' })
  })

  it('加载失败允许重试当前图片', () => {
    render(<ImagePreview open src="/broken.png" alt="证据图" onOpenChange={vi.fn()} />)
    fireEvent.error(screen.getByRole('img', { name: '证据图' }))
    expect(screen.getByRole('alert')).toHaveTextContent('图片读取失败')
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(screen.getByRole('img', { name: '证据图' })).toHaveAttribute('src', '/broken.png?preview_retry=1')
  })
})
