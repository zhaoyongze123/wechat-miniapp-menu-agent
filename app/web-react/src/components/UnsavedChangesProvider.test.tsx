import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { UnsavedChangesProvider, useUnsavedChanges } from './UnsavedChangesProvider'

function DirtyPage({ dirty }: { dirty: boolean }) {
  useUnsavedChanges(dirty, '设置中有未保存的修改。')
  return <a href="#/devices">设备</a>
}

describe('UnsavedChangesProvider', () => {
  afterEach(() => {
    window.history.replaceState(null, '', '#/')
  })

  it('有未保存修改时拦截站内链接并要求确认', () => {
    render(
      <UnsavedChangesProvider>
        <DirtyPage dirty />
      </UnsavedChangesProvider>,
    )
    const link = screen.getByRole('link', { name: '设备' })
    const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
    fireEvent(link, event)
    expect(event.defaultPrevented).toBe(true)
    expect(screen.getByText('放弃未保存的修改？')).toBeInTheDocument()
    expect(screen.getByText('设置中有未保存的修改。')).toBeInTheDocument()
  })

  it('没有未保存修改时不拦截链接', () => {
    render(
      <UnsavedChangesProvider>
        <DirtyPage dirty={false} />
      </UnsavedChangesProvider>,
    )
    const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
    fireEvent(screen.getByRole('link', { name: '设备' }), event)
    expect(event.defaultPrevented).toBe(false)
    expect(screen.queryByText('放弃未保存的修改？')).not.toBeInTheDocument()
  })

  it('浏览器后退改变 hash 时恢复原地址并要求确认', () => {
    window.history.replaceState(null, '', '#/settings')
    render(
      <UnsavedChangesProvider>
        <DirtyPage dirty />
      </UnsavedChangesProvider>,
    )
    window.location.hash = '#/devices'
    fireEvent(window, new HashChangeEvent('hashchange'))
    expect(screen.getByText('放弃未保存的修改？')).toBeInTheDocument()
    expect(window.location.hash).toBe('#/settings')
  })
})
