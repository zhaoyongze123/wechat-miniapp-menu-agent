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
  cc_global_on_worker_override: boolean
  cooldown_minutes: number
  hourly_limit: number
  daily_limit: number
  enabled_events: string[]
  http_api_url: string
  secret_configured: boolean
}

interface WorkerRecipientEntry {
  worker_id: string
  worker_name: string
  recipients: string[]
}

interface DeliveryLog {
  id: number
  event_type: string
  severity: string
  title: string
  status: string
  attempts: number
  error: string
  device_id?: string
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
  hub_synced?: boolean
  hub_email_enabled?: boolean | null
  worker_recipients?: WorkerRecipientEntry[]
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

function parseRecipients(raw: string): string[] {
  return raw
    .split(/[\n,，;；]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

export function EmailNotificationSettings() {
  const queryClient = useQueryClient()
  const [deviceFilter, setDeviceFilter] = useState('')
  const query = useQuery({
    queryKey: ['notifications-email', deviceFilter],
    queryFn: ({ signal }) =>
      apiFetch<EmailNotificationResponse>(
        deviceFilter ? `/api/notifications/email?device_id=${encodeURIComponent(deviceFilter)}` : '/api/notifications/email',
        { signal },
      ),
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
  const [ccGlobal, setCcGlobal] = useState(true)
  const [cooldownMinutes, setCooldownMinutes] = useState('30')
  const [enabledEvents, setEnabledEvents] = useState<string[]>([])
  const [workerDrafts, setWorkerDrafts] = useState<Record<string, string>>({})
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
    setCcGlobal(email.cc_global_on_worker_override !== false)
    setCooldownMinutes(String(email.cooldown_minutes))
    setEnabledEvents(email.enabled_events)
    setSecretInput('')
    setClearSecret(false)
    if (query.data?.worker_recipients) {
      const nextDrafts: Record<string, string> = {}
      for (const item of query.data.worker_recipients) {
        nextDrafts[item.worker_id] = item.recipients.join(', ')
      }
      setWorkerDrafts(nextDrafts)
    }
  }, [query.data])

  const saveMutation = useMutation({
    mutationFn: () =>
      apiFetch<EmailNotificationResponse>('/api/notifications/email', {
        method: 'PUT',
        body: JSON.stringify({
          enabled,
          provider: 'smtp',
          smtp_host: smtpHost.trim(),
          smtp_port: Number(smtpPort) || 465,
          use_ssl: useSsl,
          sender: sender.trim(),
          username: username.trim(),
          recipients: parseRecipients(recipientsText),
          cc_global_on_worker_override: ccGlobal,
          cooldown_minutes: Number(cooldownMinutes) || 30,
          hourly_limit: query.data?.email.hourly_limit ?? 10,
          daily_limit: query.data?.email.daily_limit ?? 40,
          enabled_events: enabledEvents,
          http_api_url: query.data?.email.http_api_url ?? '',
          secret: secretInput.trim(),
          clear_secret: clearSecret,
        }),
      }),
    onSuccess: (next) => {
      void queryClient.invalidateQueries({ queryKey: ['notifications-email'] })
      setSecretInput('')
      setClearSecret(false)
      setFeedbackError(false)
      if (next.managed_by_hub) {
        setFeedback(next.hub_synced ? '本机专属收件邮箱已保存并同步至 Hub。' : '已保存本机配置；当前未连接 Hub，联网后可重新保存同步。')
      } else {
        setFeedback('邮件告警配置已保存。')
      }
    },
    onError: (err) => {
      setFeedbackError(true)
      setFeedback(err instanceof Error ? err.message : '保存邮件配置失败。')
    },
  })

  const saveWorkerRecipientsMutation = useMutation({
    mutationFn: ({ workerId, raw }: { workerId: string; raw: string }) =>
      apiFetch<{ worker_id: string; recipients: string[] }>(`/api/notifications/email/workers/${encodeURIComponent(workerId)}`, {
        method: 'PUT',
        body: JSON.stringify({ recipients: parseRecipients(raw) }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['notifications-email'] })
      setFeedbackError(false)
      setFeedback('采集设备专属收件邮箱已更新。')
    },
    onError: (err) => {
      setFeedbackError(true)
      setFeedback(err instanceof Error ? err.message : '保存设备专属收件人失败。')
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
  const isHub = data?.role === 'hub'
  const isWorker = Boolean(data?.managed_by_hub)
  const eventLabels = data?.event_labels ?? {}
  const deliveries = data?.deliveries ?? []
  const workerRecipients = data?.worker_recipients ?? []
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
            {isWorker
              ? '发件凭据由共享后台 Hub 统一托管；此处设置的收件邮箱将作为本机专属告警收件人同步至 Hub，仅在断联时使用下方本机兜底 SMTP。'
              : '当微信掉线、主窗口不可用、连续环境故障或批量熔断时自动发送邮件，支持全局默认收件人与各采集设备独立收件人路由。'}
          </p>
        </div>
        <StatusBadge
          tone={
            isWorker
              ? data?.hub_email_enabled
                ? 'success'
                : 'secondary'
              : enabled && secretConfigured
                ? 'success'
                : enabled
                  ? 'warning'
                  : 'secondary'
          }
        >
          {isWorker
            ? data?.hub_email_enabled
              ? 'Hub 统一发信已启用'
              : 'Hub 发信未启用'
            : enabled && secretConfigured
              ? '已启用'
              : enabled
                ? '待配授权码'
                : '未启用'}
        </StatusBadge>
      </div>

      <SettingRow
        label={isWorker ? '本机专属收件邮箱' : '全局默认收件人邮箱'}
        description={
          isWorker
            ? '留空则回退使用 Hub 全局默认收件人；多个邮箱用逗号或换行分隔。'
            : '未单独配置专属收件人的设备将发往此处；多个邮箱用逗号或换行分隔，最多 20 个。'
        }
        htmlFor="email-recipients"
      >
        <Input
          id="email-recipients"
          value={recipientsText}
          placeholder="zhaoyongze2023@gmail.com, zhaoyongze2023@163.com, 2173058314@qq.com"
          onChange={(e) => setRecipientsText(e.target.value)}
        />
      </SettingRow>

      {isHub ? (
        <Checkbox
          label="采集设备配置了专属收件人时，仍同时抄送全局默认收件人（防漏报）"
          checked={ccGlobal}
          onCheckedChange={(checked) => setCcGlobal(checked)}
        />
      ) : null}

      <Checkbox
        label={isWorker ? '启用本机断联兜底邮件告警（仅在与 Hub 断联时生效）' : '启用关键异常邮件告警'}
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
          description={secretConfigured ? '已保存在本机 secrets.local.json（0600），留空保持不变。' : '仅存本机受限权限文件，不回传明文。'}
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

      {isHub && workerRecipients.length > 0 ? (
        <div className="space-y-2.5 border-t border-subtle pt-3">
          <div>
            <span className="text-xs font-medium text-default">各采集设备专属收件邮箱</span>
            <p className="mt-0.5 text-2xs text-tertiary">留空表示使用上方全局默认收件人；采集端本机修改也会实时同步到此表。</p>
          </div>
          <div className="space-y-2">
            {workerRecipients.map((item) => (
              <div key={item.worker_id} className="flex flex-wrap items-center gap-2">
                <span className="w-28 shrink-0 truncate text-xs text-default">{item.worker_name}</span>
                <div className="min-w-0 flex-1">
                  <Input
                    aria-label={`${item.worker_name} 专属收件邮箱`}
                    value={workerDrafts[item.worker_id] ?? ''}
                    placeholder="留空则沿用全局默认收件人"
                    onChange={(e) =>
                      setWorkerDrafts((prev) => ({
                        ...prev,
                        [item.worker_id]: e.target.value,
                      }))
                    }
                  />
                </div>
                <Button
                  type="button"
                  color="secondary"
                  variant="outline"
                  size="sm"
                  disabled={saveWorkerRecipientsMutation.isPending}
                  onClick={() =>
                    saveWorkerRecipientsMutation.mutate({
                      workerId: item.worker_id,
                      raw: workerDrafts[item.worker_id] ?? '',
                    })
                  }
                >
                  保存
                </Button>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {deliveries.length > 0 || (isHub && workerRecipients.length > 0) ? (
        <div className="space-y-2 border-t border-subtle pt-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs font-medium text-default">
              {isWorker ? '本机最近告警投递记录' : '最近告警投递记录（全局）'}
            </span>
            {isHub && workerRecipients.length > 0 ? (
              <div className="w-44">
                <Select
                  aria-label="按设备筛选投递记录"
                  value={deviceFilter}
                  options={[
                    { value: '', label: '全部设备' },
                    ...workerRecipients.map((item) => ({ value: item.worker_id, label: item.worker_name })),
                  ]}
                  onChange={(opt) => setDeviceFilter(opt.value)}
                />
              </div>
            ) : null}
          </div>
          {deliveries.length === 0 ? (
            <p className="text-xs text-tertiary">暂无投递记录。</p>
          ) : (
            <ul className="space-y-1.5">
              {deliveries.slice(0, 8).map((item) => {
                const meta = DELIVERY_STATUS_META[item.status] ?? { label: item.status, tone: 'secondary' as StatusTone }
                return (
                  <li
                    key={item.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-subtle bg-surface-secondary px-3 py-2 text-xs"
                  >
                    <div className="flex min-w-0 items-center gap-2">
                      <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
                      {isHub && item.device_name ? (
                        <span className="shrink-0 rounded bg-surface px-1.5 py-0.5 text-2xs text-secondary">{item.device_name}</span>
                      ) : null}
                      <span className="truncate text-default">{item.title}</span>
                    </div>
                    <span className="text-2xs text-tertiary">
                      {item.error ? item.error.slice(0, 60) : item.sent_at || item.created_at}
                    </span>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  )
}
