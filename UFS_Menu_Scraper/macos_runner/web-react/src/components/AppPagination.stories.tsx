import type { Meta, StoryObj } from '@storybook/react-vite'
import { useState } from 'react'
import { expect, userEvent, within } from 'storybook/test'
import { AppPagination } from './AppPagination'

function PaginationDemo() {
  const [page, setPage] = useState(1)
  const [size, setSize] = useState(20)
  return (
    <AppPagination
      page={page}
      pageCount={23}
      total={452}
      pageSize={size}
      onPageChange={setPage}
      onPageSizeChange={setSize}
    />
  )
}

const meta: Meta = {
  title: '组件/分页',
  parameters: { layout: 'padded' },
}

export default meta
type Story = StoryObj<typeof meta>

export const ManyPages: Story = {
  render: () => <PaginationDemo />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.type(canvas.getByLabelText('跳转到页码'), '9')
    await userEvent.click(canvas.getByRole('button', { name: '跳转' }))
    await expect(canvas.getByText(/第 9 \/ 23 页/)).toBeTruthy()
  },
}

export const FewPages: Story = {
  render: () => (
    <AppPagination page={2} pageCount={3} total={48} pageSize={20} onPageChange={() => undefined} />
  ),
}
