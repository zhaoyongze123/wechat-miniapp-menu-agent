import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { apiFetch } from '../../api/client'
import { SettingRow } from '../../components/SettingsLayout'
import { StatusBadge, type StatusTone } from '../../components/StatusBadge'

interface EmailNotificationConfig {
  enabled: boolean
  provider: 'smtp' | 'http_api'
  smtp_host: string
  smtp_port: number
  use_ssl: boolean
  sender: string
  username: string
  recipients: string[]
  cooldown_minutes: number
  hourly_limit: number
  daily_limit: number
  enabled_events: string[]
  http_api_url: string
  secret_configured: boolean
}

interface DeliveryLog {
  id: number
  event_type: string
  severity: string
  title: string
  status: string
  attempts: number
  error: string
  device_name: string
  created_at: string
  sent_at: string
}

interface EmailNotificationResponse {
  email: EmailNotificationConfig
  deliveries: DeliveryLog[]
  event_labels: Record<string, string>
  role: string
  managed_by_hub: boolean
}

const DELIVERY_STATUS_META: Record<string, { label: string; tone: StatusTone }> = {
  sent: { label: '已发送', tone: 'success' },
  forwarded: { label: '已上报 Hub', tone: 'info' },
  suppressed: { label: '冷静期去重', tone: 'secondary' },
  rate_limited: { label: '已限频', tone: 'warning' },
  failed: { label: '发送失败', tone: 'danger' },
}

const EVENT_ORDER = [
  'wechat_logged_out',
  'wechat_window_unavailable',
  'batch_circuit_breaker_tripped',
  'worker_offline_sustained',
  'worker_sync_credential_invalid',
  'batch_completed_summary',
]

export function EmailNotificationSettings() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['notifications-email'],
    queryFn: ({ signal }) => apiFetch<EmailNotificationResponse>('/api/notifications/email', { signal }),
    staleTime: 15_000,
  })

  const [enabled, setEnabled] = useState(false)
  const [smtpHost, setSmtpHost] = useState('')
  const [smtpPort, setSmtpPort] = useState('465')
  const [useSsl, setUseSsl] = useState(true)
  const [sender, setSender] = useState('')
  const [username, setUsername] = useState('')
  const [secretInput, setSecretInput] = useState('')
  const [clearSecret, setClearSecret] = useState(false)
  const [recipientsText, setRecipientsText] = useState('')
  const [cooldownMinutes, setCooldownMinutes] = useState('30')
  const [enabledEvents, setEnabledEvents] = useState<string[]>([])
  const [feedback, setFeedback] = useState('')
  const [feedbackError, setFeedbackError] = useState(false)

  useEffect(() => {
    const email = query.data?.email
    if (!email) return
    setEnabled(email.enabled)
    setSmtpHost(email.smtp_host)
    setSmtpPort(String(email.smtp_port))
    setUseSsl(email.use_ssl)
    setSender(email.sender)
    setUsername(email.username)
    setRecipientsText(email.recipients.join(', '))
    setCooldownMinutes(String(email.cooldown_minutes))
    setEnabledEvents(email.enabled_events)
    setSecretInput('')
    setClearSecret(false)
  }, [query.data])

  const saveMutation = useMutation({
    mutationFn: () => {
      const recipients = recipientsText
        .split(/[\n,，;；]+/)
        .map((item) => item.trim())
        .filter(Boolean)
      return apiFetch<EmailNotificationResponse>('/api/notifications/email', {
        method: 'PUT',
        body: JSON.stringify({
          enabled,
          provider: 'smtp',
          smtp_host: smtpHost.trim(),
          smtp_port: Number(smtpPort) || 465,
          use_ssl: useSsl,
          sender: sender.trim(),
          username: username.trim(),
          recipients,
          cooldown_minutes: Number(cooldownMinutes) || 30,
          hourly_limit: query.data?.email.hourly_limit ?? 10,
          daily_limit: query.data?.email.daily_limit ?? 40,
          enabled_events: enabledEvents,
          http_api_url: query.data?.email.http_api_url ?? '',
          secret: secretInput.trim(),
          clear_secret: clearSecret,
        }),
      })
    },
    onSuccess: (next) => {
      queryClient.setQueryData(['notifications-email'], next)
      setSecretInput('')
      setClearSecret(false)
      setFeedbackError(false)
      setFeedback('邮件告警配置已保存。')
    },
    onError: (err) => {
      setFeedbackError(true)
      setFeedback(err instanceof Error ? err.message : '保存邮件配置失败。')
    },
  })

  const testMutation = useMutation({
    mutationFn: () =>
      apiFetch<{ ok: boolean; recipients: string[]; sent_at: string }>('/api/notifications/email/test', {
        method: 'POST',
      }),
    onSuccess: (res) => {
      void queryClient.invalidateQueries({ queryKey: ['notifications-email'] })
      setFeedbackError(false)
      setFeedback(`测试邮件已发送至 ${res.recipients.join('、')}。`)
    },
    onError: (err) => {
      void queryClient.invalidateQueries({ queryKey: ['notifications-email'] })
      setFeedbackError(true)
      setFeedback(err instanceof Error ? err.message : '测试邮件发送失败。')
    },
  })

  const data = query.data
  const eventLabels = data?.event_labels ?? {}
  const deliveries = data?.deliveries ?? []
  const secretConfigured = Boolean(data?.email.secret_configured)

  const toggleEvent = (key: string, checked: boolean) => {
    setEnabledEvents((prev) => (checked ? Array.from(new Set([...prev, key])) : prev.filter((item) => item !== key)))
  }

  return (
    <div className="space-y-4 border-t border-subtle pt-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm text-default">邮件告警渠道（服务端无人值守通知）</h3>
          <p className="mt-1 text-xs text-secondary">
            {data?.managed_by_hub
              ? '共享模式下由 Hub 统一发信，采集端关键异常会自动上报至 Hub；仅在断联兜底时使用本机配置。'
              : '当微信掉线、主窗口不可用、连续环境故障或批量熔断时自动发送邮件，内置去重、冷静期与限频保护。'}
          </p>
        </div>
        <StatusBadge tone={enabled && secretConfigured ? 'success' : enabled ? 'warning' : 'secondary'}>
          {enabled && secretConfigured ? '已启用' : enabled ? '待配授权码' : '未启用'}
        </StatusBadge>
      </div>

      <Checkbox
        label="启用关键异常邮件告警"
        checked={enabled}
        onCheckedChange={(checked) => setEnabled(checked)}
      />

      <div className="grid gap-3 sm:grid-cols-2">
        <SettingRow label="SMTP 服务器" description="163 邮箱：smtp.163.com；QQ 邮箱：smtp.qq.com" htmlFor="email-smtp-host">
          <Input
            id="email-smtp-host"
            value={smtpHost}
            placeholder="smtp.163.com"
            onChange={(e) => setSmtpHost(e.target.value)}
          />
        </SettingRow>
        <SettingRow label="端口与加密" description="SSL 推荐 465；STARTTLS 推荐 587" htmlFor="email-smtp-port">
          <div className="flex items-center gap-2">
            <Input
              id="email-smtp-port"
              value={smtpPort}
              placeholder="465"
              onChange={(e) => setSmtpPort(e.target.value)}
            />
            <Checkbox label="SSL" checked={useSsl} onCheckedChange={(checked) => setUseSsl(checked)} />
          </div>
        </SettingRow>
        <SettingRow label="发件人邮箱" htmlFor="email-sender">
          <Input
            id="email-sender"
            value={sender}
            placeholder="example@163.com"
            onChange={(e) => setSender(e.target.value)}
          />
        </SettingRow>
        <SettingRow label="登录账号（可选）" description="留空则默认与发件人邮箱一致" htmlFor="email-username">
          <Input
            id="email-username"
            value={username}
            placeholder="留空同发件人邮箱"
            onChange={(e) => setUsername(e.target.value)}
          />
        </SettingRow>
        <SettingRow
          label="邮箱授权码 / 密码"
          description={secretConfigured ? '已加密保存在本机 secrets.local.json（0600），留空保持不变。' : '仅存本机受限权限文件，不回传明文。'}
          htmlFor="email-secret"
        >
          <div className="space-y-1.5">
            <Input
              id="email-secret"
              type="password"
              value={secretInput}
              placeholder={secretConfigured ? '已配置（输入新值以替换）' : '输入邮箱 SMTP 授权码'}
              onChange={(e) => {
                setSecretInput(e.target.value)
                if (e.target.value) setClearSecret(false)
              }}
            />
            {secretConfigured ? (
              <Checkbox
                label="保存时清除已存授权码"
                checked={clearSecret}
                onCheckedChange={(checked) => setClearSecret(checked)}
              />
            ) : null}
          </div>
        </SettingRow>
        <SettingRow label="同类告警冷静期" description="同一设备/轮次同类异常在冷静期内不重复发信" htmlFor="email-cooldown">
          <Select
            id="email-cooldown"
            aria-label="同类告警冷静期"
            value={cooldownMinutes}
            options={[
              { value: '10', label: '10 分钟' },
              { value: '30', label: '30 分钟（推荐）' },
              { value: '60', label: '60 分钟' },
              { value: '120', label: '120 分钟' },
            ]}
            onChange={(opt) => setCooldownMinutes(opt.value)}
          />
        </SettingRow>
      </div>

      <SettingRow label="收件人邮箱" description="多个邮箱用逗号或换行分隔，最多 20 个" htmlFor="email-recipients">
        <Input
          id="email-recipients"
          value={recipientsText}
          placeholder="zhaoyongze2023@gmail.com, zhaoyongze2023@163.com, 2173058314@qq.com"
          onChange={(e) => setRecipientsText(e.target.value)}
        />
      </SettingRow>

      <div className="space-y-2">
        <span className="text-xs font-medium text-default">触发邮件的告警事件</span>
        <div className="grid gap-2 sm:grid-cols-2">
          {EVENT_ORDER.map((key) => (
            <Checkbox
              key={key}
              label={eventLabels[key] || key}
              checked={enabledEvents.includes(key)}
              onCheckedChange={(checked) => toggleEvent(key, checked)}
            />
          ))}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
        <p className={`text-xs ${feedbackError ? 'text-danger' : 'text-secondary'}`} role="status" aria-live="polite">
          {feedback}
        </p>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            color="secondary"
            variant="outline"
            size="sm"
            disabled={testMutation.isPending || saveMutation.isPending || !secretConfigured}
            onClick={() => testMutation.mutate()}
          >
            {testMutation.isPending ? '发送中…' : '发送测试邮件'}
          </Button>
          <Button
            type="button"
            color="primary"
            size="sm"
            disabled={saveMutation.isPending}
            onClick={() => saveMutation.mutate()}
          >
            {saveMutation.isPending ? '保存中…' : '保存邮件配置'}
          </Button>
        </div>
      </div>

      {deliveries.length > 0 ? (
        <div className="space-y-2 border-t border-subtle pt-3">
          <span className="text-xs font-medium text-default">最近投递记录</span>
          <ul className="space-y-1.5">
            {deliveries.slice(0, 5).map((item) => {
              const meta = DELIVERY_STATUS_META[item.status] ?? { label: item.status, tone: 'secondary' as StatusTone }
              return (
                <li
                  key={item.id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-subtle bg-surface-secondary px-3 py-2 text-xs"
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
                    <span className="truncate text-default">{item.title}</span>
                  </div>
                  <span className="text-2xs text-tertiary">
                    {item.error ? item.error.slice(0, 60) : item.sent_at || item.created_at}
                  </span>
                </li>
              )
            })}
          </ul>
        </div>
      ) : null}
    </div>
  )
}
