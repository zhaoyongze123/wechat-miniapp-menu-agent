import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider, useToast } from './ToastProvider'

function Demo() {
  const toast = useToast()
  return (
    <>
      <button type="button" onClick={() => toast.push('配置已保存', 'success')}>
        成功
      </button>
      <button type="button" onClick={() => toast.push('同步失败，请检查网络', 'danger')}>
        失败
      </button>
    </>
  )
}

function renderToasts() {
  return render(
    <ToastProvider>
      <Demo />
    </ToastProvider>,
  )
}

describe('ToastProvider', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('成功提示短暂展示后自动消失', () => {
    vi.useFakeTimers()
    renderToasts()
    fireEvent.click(screen.getByText('成功'))
    expect(screen.getByText('配置已保存')).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(3100))
    expect(screen.queryByText('配置已保存')).not.toBeInTheDocument()
  })

  it('失败提示停留更久并可手动关闭', () => {
    vi.useFakeTimers()
    renderToasts()
    fireEvent.click(screen.getByText('失败'))
    act(() => vi.advanceTimersByTime(5000))
    expect(screen.getByText('同步失败，请检查网络')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('关闭提示'))
    expect(screen.queryByText('同步失败，请检查网络')).not.toBeInTheDocument()
    vi.useRealTimers()
  })

  it('相同提示重复触发时只保留一个', () => {
    renderToasts()
    fireEvent.click(screen.getByText('成功'))
    fireEvent.click(screen.getByText('成功'))
    expect(screen.getAllByText('配置已保存')).toHaveLength(1)
  })
})
