import type { Meta, StoryObj } from '@storybook/react-vite'
import { useState } from 'react'
import { expect, userEvent, within } from 'storybook/test'
import { ImagePreview } from './ImagePreview'

const pixel =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2Z4sAAAAASUVORK5CYII='

function PreviewDemo() {
  const [open, setOpen] = useState(true)
  return (
    <ImagePreview
      open={open}
      src={pixel}
      alt="示例证据"
      items={[
        { src: pixel, alt: '第一张证据', caption: '第一张证据' },
        { src: pixel, alt: '第二张证据', caption: '第二张证据' },
      ]}
      onOpenChange={setOpen}
    />
  )
}

const meta: Meta = {
  title: '组件/图片预览',
  parameters: { layout: 'fullscreen' },
}

export default meta
type Story = StoryObj<typeof meta>

export const Sequence: Story = {
  render: () => <PreviewDemo />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText(/1 \/ 2/)).toBeTruthy()
    await userEvent.click(canvas.getByRole('button', { name: '下一张' }))
    await expect(canvas.getByText(/2 \/ 2/)).toBeTruthy()
    await userEvent.click(canvas.getByRole('button', { name: '上一张' }))
    await expect(canvas.getByText(/1 \/ 2/)).toBeTruthy()
  },
}
