import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../../components/ToastProvider'
import { UnsavedChangesProvider } from '../../components/UnsavedChangesProvider'
import { EmailNotificationSettings } from './EmailNotificationSettings'

const BASE_EMAIL = {
  enabled: true,
  provider: 'smtp' as const,
  smtp_host: 'smtp.163.com',
  smtp_port: 465,
  use_ssl: true,
  sender: 'sender@example.com',
  username: '',
  recipients: ['global@example.com'],
  cc_global_on_worker_override: true,
  cooldown_minutes: 30,
  hourly_limit: 10,
  daily_limit: 40,
  enabled_events: ['wechat_logged_out', 'batch_circuit_breaker_tripped'],
  http_api_url: '',
  secret_configured: true,
}

function renderWithProviders() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <UnsavedChangesProvider>
          <EmailNotificationSettings />
        </UnsavedChangesProvider>
      </ToastProvider>
    </QueryClientProvider>,
  )
}

describe('EmailNotificationSettings', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('Worker 模式折叠本机兜底 SMTP 并支持经由 Hub 发送测试邮件', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input instanceof Request ? input.url : String(input)
      if (url.includes('/api/session')) {
        return new Response(JSON.stringify({ token: 'test-token' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.includes('/api/notifications/email/test') && init?.method === 'POST') {
        return new Response(
          JSON.stringify({ ok: true, recipients: ['worker@example.com'], sent_at: '2026-11-28T14:50:00Z' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }
      return new Response(
        JSON.stringify({
          email: { ...BASE_EMAIL, enabled: false, recipients: ['worker@example.com'], secret_configured: false },
          deliveries: [],
          event_labels: { wechat_logged_out: '微信退出登录' },
          role: 'worker',
          managed_by_hub: true,
          hub_synced: true,
          hub_email_enabled: true,
          hub_cc_global: true,
          hub_global_recipients_count: 1,
          worker_recipients: [],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      )
    })

    renderWithProviders()
    expect(await screen.findByText('Hub 统一发信已启用')).toBeInTheDocument()
    expect(screen.getByText(/已设 1 个本机专属邮箱，触发告警时将同时抄送 Hub 全局收件人/)).toBeInTheDocument()

    const details = screen.getByText(/本机断联兜底 SMTP/).closest('details')
    expect(details).not.toHaveAttribute('open')

    const testBtn = screen.getByRole('button', { name: '经由 Hub 测试发信' })
    expect(testBtn).toBeEnabled()
    fireEvent.click(testBtn)

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/notifications/email/test',
        expect.objectContaining({ method: 'POST' }),
      )
    })
  })

  it('Hub 模式校验非法邮箱、保留未保存草稿不被设备筛选覆盖，并支持按设备测试', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input instanceof Request ? input.url : String(input)
      if (url.includes('/api/session')) {
        return new Response(JSON.stringify({ token: 'test-token' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.includes('/api/notifications/email/test') && init?.method === 'POST') {
        return new Response(
          JSON.stringify({ ok: true, recipients: ['a@example.com'], sent_at: '2026-11-28T14:55:00Z' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }
      return new Response(
        JSON.stringify({
          email: BASE_EMAIL,
          deliveries: [
            {
              id: 1,
              event_type: 'batch_circuit_breaker_tripped',
              severity: 'critical',
              title: '批量采集熔断',
              status: 'sent',
              attempts: 1,
              error: '',
              device_id: 'w1',
              device_name: 'A机',
              created_at: '2026-11-28T14:00:00Z',
              sent_at: '2026-11-28T14:00:02Z',
              recipients: ['a@example.com'],
            },
          ],
          event_labels: { wechat_logged_out: '微信退出登录', batch_circuit_breaker_tripped: '批量采集连续故障熔断' },
          role: 'hub',
          managed_by_hub: false,
          worker_recipients: [{ worker_id: 'w1', worker_name: 'A机', recipients: ['a@example.com'] }],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      )
    })

    renderWithProviders()
    const globalInput = await screen.findByLabelText('全局默认收件人邮箱')
    fireEvent.change(globalInput, { target: { value: 'invalid-mail' } })
    expect(screen.getByText(/邮箱格式无效：invalid-mail/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '保存邮件配置' })).toBeDisabled()

    // 修复为合法草稿后刷新记录，草稿不应被服务端响应重置
    fireEvent.change(globalInput, { target: { value: 'draft@example.com' } })
    fireEvent.click(screen.getByRole('button', { name: '刷新记录' }))
    await waitFor(() => {
      expect((screen.getByLabelText('全局默认收件人邮箱') as HTMLInputElement).value).toBe('draft@example.com')
    })

    // 按设备测试
    fireEvent.click(screen.getByRole('button', { name: '测试' }))
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/notifications/email/test',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({ worker_id: 'w1' }),
        }),
      )
    })
  })
})
