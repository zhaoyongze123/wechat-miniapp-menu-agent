import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../../components/ToastProvider'
import { NotificationRuntime } from '../notifications/NotificationRuntime'
import { RemoteControlPage } from './RemoteControlPage'

describe('RemoteControlPage', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    window.history.replaceState({}, '', '/')
  })

  it('renders RemoteControlPage + NotificationRuntime cleanly outside ProjectProvider without white screen', async () => {
    window.history.replaceState({}, '', '/remote-control')
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          device_id: 'dev-1',
          device_name: '共享端A机',
          project_id: '',
          manual: null,
          run_name: '',
          run_status: '',
          remote_url: 'https://example.ts.net/remote-control',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <RemoteControlPage />
          <NotificationRuntime />
        </ToastProvider>
      </QueryClientProvider>,
    )

    expect(await screen.findByText('当前没有运行中的半自动采集任务')).toBeInTheDocument()
  })
})
