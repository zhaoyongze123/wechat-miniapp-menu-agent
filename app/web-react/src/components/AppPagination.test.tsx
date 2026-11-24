import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { AppPagination, pageWindow } from './AppPagination'

describe('AppPagination', () => {
  it('页码窗口始终包含当前页且不超过页数', () => {
    expect(pageWindow(1, 23)).toEqual([1, 2, 3, 4, 5, 6, 7])
    expect(pageWindow(12, 23)).toEqual([9, 10, 11, 12, 13, 14, 15])
    expect(pageWindow(23, 23)).toEqual([17, 18, 19, 20, 21, 22, 23])
    expect(pageWindow(2, 3)).toEqual([1, 2, 3])
  })

  it('大页数支持首末页与快速跳转', async () => {
    const onPageChange = vi.fn()
    const user = userEvent.setup()
    render(<AppPagination page={5} pageCount={23} total={452} pageSize={20} onPageChange={onPageChange} />)

    expect(screen.getByText(/第 5 \/ 23 页/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '首页' }))
    expect(onPageChange).toHaveBeenCalledWith(1)
    await user.click(screen.getByRole('button', { name: '末页' }))
    expect(onPageChange).toHaveBeenCalledWith(23)

    onPageChange.mockClear()
    await user.type(screen.getByLabelText('跳转到页码'), '12')
    await user.click(screen.getByRole('button', { name: '跳转' }))
    expect(onPageChange).toHaveBeenCalledWith(12)
  })

  it('跳转页码会被限制在有效范围内', async () => {
    const onPageChange = vi.fn()
    const user = userEvent.setup()
    render(<AppPagination page={3} pageCount={8} total={160} pageSize={20} onPageChange={onPageChange} />)

    await user.type(screen.getByLabelText('跳转到页码'), '99')
    await user.click(screen.getByRole('button', { name: '跳转' }))
    expect(onPageChange).toHaveBeenCalledWith(8)
  })

  it('页数较少时不显示快速跳转', () => {
    render(<AppPagination page={1} pageCount={3} total={48} pageSize={20} onPageChange={() => undefined} />)
    expect(screen.queryByLabelText('跳转到页码')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '首页' })).not.toBeInTheDocument()
  })
})
