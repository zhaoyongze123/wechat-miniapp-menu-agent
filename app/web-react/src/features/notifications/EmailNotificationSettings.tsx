import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { apiFetch, errorMessage } from '../../api/client'
import { SettingRow } from '../../components/SettingsLayout'
import { StatusBadge, type StatusTone } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { useUnsavedChanges } from '../../components/UnsavedChangesProvider'
import { formatTime } from '../hub/format'

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
  recipients?: string[]
}

interface EmailNotificationResponse {
  email: EmailNotificationConfig
  deliveries: DeliveryLog[]
  event_labels: Record<string, string>
  role: string
  managed_by_hub: boolean
  hub_synced?: boolean
  hub_email_enabled?: boolean | null
  hub_cc_global?: boolean | null
  hub_global_recipients_count?: number | null
  worker_recipients?: WorkerRecipientEntry[]
}

interface EmailDraft {
  enabled: boolean
  smtpHost: string
  smtpPort: string
  useSsl: boolean
  sender: string
  username: string
  recipientsText: string
  ccGlobal: boolean
  cooldownMinutes: string
  hourlyLimit: string
  dailyLimit: string
  enabledEvents: string[]
}

const DELIVERY_STATUS_META: Record<string, { label: string; tone: StatusTone }> = {
  sent: { label: '已发送', tone: 'success' },
  forwarded: { label: '已上报 Hub', tone: 'info' },
  suppressed: { label: '已抑制', tone: 'secondary' },
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

const SMTP_PRESETS = [
  { value: 'custom', label: '自定义 SMTP 服务器', host: '', port: '465', useSsl: true },
  { value: '163', label: '网易 163 邮箱（smtp.163.com）', host: 'smtp.163.com', port: '465', useSsl: true },
  { value: 'qq', label: 'QQ 邮箱（smtp.qq.com）', host: 'smtp.qq.com', port: '465', useSsl: true },
  { value: 'exmail', label: '腾讯企业邮（smtp.exmail.qq.com）', host: 'smtp.exmail.qq.com', port: '465', useSsl: true },
  { value: 'aliyun', label: '阿里企业邮（smtp.qiye.aliyun.com）', host: 'smtp.qiye.aliyun.com', port: '465', useSsl: true },
] as const

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

function parseRecipients(raw: string): string[] {
  const result: string[] = []
  const seen = new Set<string>()
  for (const part of raw.split(/[\n,，;；]+/)) {
    const item = part.trim()
    if (!item) continue
    const lower = item.toLowerCase()
    if (!seen.has(lower)) {
      seen.add(lower)
      result.push(item)
    }
  }
  return result
}

function findInvalidEmails(emails: string[]): string[] {
  return emails.filter((item) => !EMAIL_RE.test(item))
}

function draftFromConfig(email: EmailNotificationConfig): EmailDraft {
  return {
    enabled: email.enabled,
    smtpHost: email.smtp_host,
    smtpPort: String(email.smtp_port),
    useSsl: email.use_ssl,
    sender: email.sender,
    username: email.username,
    recipientsText: email.recipients.join(', '),
    ccGlobal: email.cc_global_on_worker_override !== false,
    cooldownMinutes: String(email.cooldown_minutes),
    hourlyLimit: String(email.hourly_limit),
    dailyLimit: String(email.daily_limit),
    enabledEvents: [...email.enabled_events],
  }
}

function detectPreset(host: string, port: string, useSsl: boolean): string {
  const normalized = host.trim().toLowerCase()
  const matched = SMTP_PRESETS.find(
    (item) => item.value !== 'custom' && item.host === normalized && item.port === port.trim() && item.useSsl === useSsl,
  )
  return matched ? matched.value : 'custom'
}

export function EmailNotificationSettings() {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [deviceFilter, setDeviceFilter] = useState('')
  const [showAllDeliveries, setShowAllDeliveries] = useState(false)

  const query = useQuery({
    queryKey: ['notifications-email', deviceFilter],
    queryFn: ({ signal }) =>
      apiFetch<EmailNotificationResponse>(
        deviceFilter ? `/api/notifications/email?device_id=${encodeURIComponent(deviceFilter)}` : '/api/notifications/email',
        { signal },
      ),
    staleTime: 15_000,
  })

  const [baseline, setBaseline] = useState<EmailDraft | null>(null)
  const [draft, setDraft] = useState<EmailDraft | null>(null)
  const [secretInput, setSecretInput] = useState('')
  const [clearSecret, setClearSecret] = useState(false)
  const [workerDrafts, setWorkerDrafts] = useState<Record<string, string>>({})
  const [savingWorkerId, setSavingWorkerId] = useState<string | null>(null)
  const [testingWorkerId, setTestingWorkerId] = useState<string | null>(null)
  const [feedback, setFeedback] = useState('')
  const [feedbackError, setFeedbackError] = useState(false)

  const applyServerSnapshot = (next: EmailNotificationResponse, { resetDraft }: { resetDraft: boolean }) => {
    const nextBaseline = draftFromConfig(next.email)
    if (resetDraft) {
      setBaseline(nextBaseline)
      setDraft(nextBaseline)
      setSecretInput('')
      setClearSecret(false)
    }
    if (next.worker_recipients) {
      setWorkerDrafts((prev) => {
        const merged: Record<string, string> = {}
        for (const item of next.worker_recipients ?? []) {
          merged[item.worker_id] = resetDraft || !(item.worker_id in prev) ? item.recipients.join(', ') : (prev[item.worker_id] ?? '')
        }
        return merged
      })
    }
  }

  useEffect(() => {
    const data = query.data
    if (!data?.email) return
    applyServerSnapshot(data, { resetDraft: baseline === null })
  }, [baseline, query.data])

  const dirty = Boolean(
    baseline &&
      draft &&
      (JSON.stringify(draft) !== JSON.stringify(baseline) || secretInput.trim().length > 0 || clearSecret),
  )
  useUnsavedChanges(dirty, '邮件告警配置中有未保存的修改，离开后这些修改将丢失。')

  const patchDraft = (partial: Partial<EmailDraft>) => {
    setDraft((current) => (current ? { ...current, ...partial } : current))
  }

  const parsedRecipients = useMemo(() => parseRecipients(draft?.recipientsText ?? ''), [draft?.recipientsText])
  const invalidRecipients = useMemo(() => findInvalidEmails(parsedRecipients), [parsedRecipients])
  const invalidSender = Boolean(draft?.sender.trim() && !EMAIL_RE.test(draft.sender.trim()))

  const saveMutation = useMutation({
    mutationFn: () => {
      if (!draft) throw new Error('配置尚未加载')
      if (invalidRecipients.length > 0) {
        throw new Error(`存在无效收件人邮箱：${invalidRecipients.join('、')}`)
      }
      if (invalidSender) {
        throw new Error('发件人邮箱格式无效')
      }
      const smtpPortNum = Number(draft.smtpPort) || 465
      const cooldownNum = Number(draft.cooldownMinutes) || 30
      const hourlyNum = Math.min(100, Math.max(1, Number(draft.hourlyLimit) || 10))
      const dailyNum = Math.min(500, Math.max(1, Number(draft.dailyLimit) || 40))

      return apiFetch<EmailNotificationResponse>('/api/notifications/email', {
        method: 'PUT',
        json: {
          enabled: draft.enabled,
          provider: 'smtp',
          smtp_host: draft.smtpHost.trim(),
          smtp_port: smtpPortNum,
          use_ssl: draft.useSsl,
          sender: draft.sender.trim(),
          username: draft.username.trim(),
          recipients: parsedRecipients,
          cc_global_on_worker_override: draft.ccGlobal,
          cooldown_minutes: cooldownNum,
          hourly_limit: hourlyNum,
          daily_limit: dailyNum,
          enabled_events: draft.enabledEvents,
          http_api_url: query.data?.email.http_api_url ?? '',
          secret: secretInput.trim(),
          clear_secret: clearSecret,
        },
      })
    },
    onSuccess: (next) => {
      applyServerSnapshot(next, { resetDraft: true })
      queryClient.setQueryData(['notifications-email', deviceFilter], next)
      void queryClient.invalidateQueries({ queryKey: ['notifications-email'] })
      setFeedbackError(false)
      const msg = next.managed_by_hub
        ? next.hub_synced
          ? '本机专属收件邮箱已保存并同步至 Hub。'
          : '已保存本机配置；当前未连接 Hub，联网后可重新保存同步。'
        : '邮件告警配置已保存。'
      setFeedback(msg)
      toast.push(msg, 'success')
    },
    onError: (err) => {
      const msg = errorMessage(err)
      setFeedbackError(true)
      setFeedback(msg)
      toast.push(msg, 'danger')
    },
  })

  const saveWorkerRecipientsMutation = useMutation({
    mutationFn: async ({ workerId, raw }: { workerId: string; raw: string }) => {
      const parsed = parseRecipients(raw)
      const bad = findInvalidEmails(parsed)
      if (bad.length > 0) {
        throw new Error(`设备专属邮箱格式无效：${bad.join('、')}`)
      }
      setSavingWorkerId(workerId)
      return apiFetch<{ worker_id: string; recipients: string[]; worker_recipients?: WorkerRecipientEntry[] }>(
        `/api/notifications/email/workers/${encodeURIComponent(workerId)}`,
        {
          method: 'PUT',
          json: { recipients: parsed },
        },
      )
    },
    onSuccess: (res) => {
      setWorkerDrafts((prev) => ({
        ...prev,
        [res.worker_id]: res.recipients.join(', '),
      }))
      void queryClient.invalidateQueries({ queryKey: ['notifications-email'] })
      setFeedbackError(false)
      const msg = '采集设备专属收件邮箱已更新。'
      setFeedback(msg)
      toast.push(msg, 'success')
    },
    onError: (err) => {
      const msg = errorMessage(err)
      setFeedbackError(true)
      setFeedback(msg)
      toast.push(msg, 'danger')
    },
    onSettled: () => setSavingWorkerId(null),
  })

  const testMutation = useMutation({
    mutationFn: (opts?: { workerId?: string; localOnly?: boolean }) => {
      if (opts?.workerId) setTestingWorkerId(opts.workerId)
      return apiFetch<{ ok: boolean; recipients: string[]; sent_at: string }>('/api/notifications/email/test', {
        method: 'POST',
        json: {
          ...(opts?.workerId ? { worker_id: opts.workerId } : {}),
          ...(opts?.localOnly ? { local_only: true } : {}),
        },
      })
    },
    onSuccess: (res) => {
      void queryClient.invalidateQueries({ queryKey: ['notifications-email'] })
      setFeedbackError(false)
      const target = res.recipients.length > 0 ? res.recipients.join('、') : '已配置的收件人'
      const msg = `测试邮件已发送至 ${target}。`
      setFeedback(msg)
      toast.push(msg, 'success')
    },
    onError: (err) => {
      void queryClient.invalidateQueries({ queryKey: ['notifications-email'] })
      const msg = errorMessage(err)
      setFeedbackError(true)
      setFeedback(msg)
      toast.push(msg, 'danger')
    },
    onSettled: () => setTestingWorkerId(null),
  })

  if (query.isPending && !draft) {
    return <p className="text-xs text-secondary">正在读取邮件告警配置…</p>
  }
  if (query.isError && !draft) {
    return (
      <Alert
        color="danger"
        variant="soft"
        title="邮件告警配置读取失败"
        description={errorMessage(query.error)}
      />
    )
  }
  if (!draft) return null

  const data = query.data
  const isHub = data?.role === 'hub'
  const isWorker = Boolean(data?.managed_by_hub)
  const eventLabels = data?.event_labels ?? {}
  const deliveries = data?.deliveries ?? []
  const workerRecipients = data?.worker_recipients ?? []
  const secretConfigured = Boolean(data?.email.secret_configured)
  const presetValue = detectPreset(draft.smtpHost, draft.smtpPort, draft.useSsl)
  const visibleDeliveries = showAllDeliveries ? deliveries : deliveries.slice(0, 8)

  const toggleEvent = (key: string, checked: boolean) => {
    patchDraft({
      enabledEvents: checked
        ? Array.from(new Set([...draft.enabledEvents, key]))
        : draft.enabledEvents.filter((item) => item !== key),
    })
  }

  const resetDraft = () => {
    if (!baseline) return
    setDraft(baseline)
    setSecretInput('')
    setClearSecret(false)
    setFeedback('')
    setFeedbackError(false)
  }

  const workerRouteSummary = (() => {
    if (!isWorker) return ''
    if (!data?.hub_synced) return '当前未连接共享后台 Hub，暂无法通过 Hub 发送告警；可点击保存先缓存本机收件人。'
    const ownCount = parsedRecipients.length
    const globalCount = data.hub_global_recipients_count ?? 0
    if (ownCount === 0) {
      return globalCount > 0
        ? `当前未设本机专属邮箱，告警将发往 Hub 全局默认收件人（${globalCount} 个）。`
        : '当前未设本机专属邮箱且 Hub 尚未配置全局收件人。'
    }
    return data.hub_cc_global
      ? `已设 ${ownCount} 个本机专属邮箱，触发告警时将同时抄送 Hub 全局收件人（防漏报）。`
      : `已设 ${ownCount} 个本机专属邮箱，触发告警时仅发往本机专属收件人。`
  })()

  const smtpFields = (
    <>
      <SettingRow
        label="常用邮箱预设"
        description="选择常见邮箱自动填充服务器与 SSL 端口"
        htmlFor="email-smtp-preset"
      >
        <Select
          id="email-smtp-preset"
          aria-label="常用邮箱预设"
          value={presetValue}
          options={SMTP_PRESETS.map((item) => ({ value: item.value, label: item.label }))}
          onChange={(opt) => {
            const preset = SMTP_PRESETS.find((item) => item.value === opt.value)
            if (!preset || preset.value === 'custom') return
            patchDraft({
              smtpHost: preset.host,
              smtpPort: preset.port,
              useSsl: preset.useSsl,
            })
          }}
        />
      </SettingRow>

      <div className="grid gap-3 sm:grid-cols-2">
        <SettingRow label="SMTP 服务器" description="163 邮箱：smtp.163.com；QQ 邮箱：smtp.qq.com" htmlFor="email-smtp-host">
          <Input
            id="email-smtp-host"
            value={draft.smtpHost}
            placeholder="smtp.163.com"
            onChange={(e) => patchDraft({ smtpHost: e.target.value })}
          />
        </SettingRow>
        <SettingRow label="端口与加密" description="SSL 推荐 465；STARTTLS 推荐 587" htmlFor="email-smtp-port">
          <div className="flex items-center gap-2">
            <Input
              id="email-smtp-port"
              value={draft.smtpPort}
              placeholder="465"
              onChange={(e) => patchDraft({ smtpPort: e.target.value })}
            />
            <Checkbox label="SSL" checked={draft.useSsl} onCheckedChange={(checked) => patchDraft({ useSsl: checked })} />
          </div>
        </SettingRow>
        <SettingRow label="发件人邮箱" htmlFor="email-sender">
          <div className="space-y-1">
            <Input
              id="email-sender"
              value={draft.sender}
              placeholder="alerts@example.com"
              onChange={(e) => patchDraft({ sender: e.target.value })}
            />
            {invalidSender ? <p className="text-2xs text-danger">发件人邮箱格式无效</p> : null}
          </div>
        </SettingRow>
        <SettingRow label="登录账号（可选）" description="留空则默认与发件人邮箱一致" htmlFor="email-username">
          <Input
            id="email-username"
            value={draft.username}
            placeholder="留空同发件人邮箱"
            onChange={(e) => patchDraft({ username: e.target.value })}
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
                const val = e.target.value
                setSecretInput(val)
                if (val) setClearSecret(false)
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
            value={draft.cooldownMinutes}
            options={[
              { value: '10', label: '10 分钟' },
              { value: '30', label: '30 分钟（推荐）' },
              { value: '60', label: '60 分钟' },
              { value: '120', label: '120 分钟' },
            ]}
            onChange={(opt) => patchDraft({ cooldownMinutes: opt.value })}
          />
        </SettingRow>
        <SettingRow label="每小时发信上限" description="1–100 封；防止异常风暴刷屏" htmlFor="email-hourly-limit">
          <Input
            id="email-hourly-limit"
            type="number"
            min={1}
            max={100}
            value={draft.hourlyLimit}
            onChange={(e) => patchDraft({ hourlyLimit: e.target.value })}
          />
        </SettingRow>
        <SettingRow label="每日发信上限" description="1–500 封" htmlFor="email-daily-limit">
          <Input
            id="email-daily-limit"
            type="number"
            min={1}
            max={500}
            value={draft.dailyLimit}
            onChange={(e) => patchDraft({ dailyLimit: e.target.value })}
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
              checked={draft.enabledEvents.includes(key)}
              onCheckedChange={(checked) => toggleEvent(key, checked)}
            />
          ))}
        </div>
      </div>
    </>
  )

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-secondary">
          {isWorker
            ? '发件凭据由共享后台 Hub 统一托管；此处设置的收件邮箱将作为本机专属告警收件人同步至 Hub。'
            : '支持全局默认收件人与各采集设备独立收件人路由，发件授权码保存在服务端受限权限文件。'}
        </p>
        <StatusBadge
          tone={
            isWorker
              ? !data?.hub_synced
                ? 'secondary'
                : data?.hub_email_enabled
                  ? 'success'
                  : 'warning'
              : draft.enabled && secretConfigured
                ? 'success'
                : draft.enabled
                  ? 'warning'
                  : 'secondary'
          }
        >
          {isWorker
            ? !data?.hub_synced
              ? '未连接 Hub'
              : data?.hub_email_enabled
                ? 'Hub 统一发信已启用'
                : 'Hub 发信未启用'
            : draft.enabled && secretConfigured
              ? '已启用'
              : draft.enabled
                ? '待配授权码'
                : '未启用'}
        </StatusBadge>
      </div>

      {isWorker && workerRouteSummary ? (
        <Alert
          color={!data?.hub_synced ? 'warning' : data?.hub_email_enabled ? 'info' : 'warning'}
          variant="soft"
          title={workerRouteSummary}
        />
      ) : null}

      <SettingRow
        label={isWorker ? '本机专属收件邮箱' : '全局默认收件人邮箱'}
        description={
          isWorker
            ? '留空则回退使用 Hub 全局默认收件人；多个邮箱用逗号或换行分隔。'
            : '未单独配置专属收件人的设备将发往此处；多个邮箱用逗号或换行分隔，最多 20 个。'
        }
        htmlFor="email-recipients"
      >
        <div className="space-y-1">
          <Input
            id="email-recipients"
            value={draft.recipientsText}
            placeholder="ops@example.com, alert@example.com"
            onChange={(e) => patchDraft({ recipientsText: e.target.value })}
          />
          {invalidRecipients.length > 0 ? (
            <p className="text-2xs text-danger">邮箱格式无效：{invalidRecipients.join('、')}</p>
          ) : null}
        </div>
      </SettingRow>

      {isHub ? (
        <Checkbox
          label="采集设备配置了专属收件人时，仍同时抄送全局默认收件人（防漏报）"
          checked={draft.ccGlobal}
          onCheckedChange={(checked) => patchDraft({ ccGlobal: checked })}
        />
      ) : null}

      {isWorker ? (
        <details open={draft.enabled} className="rounded-lg border border-subtle px-3 py-2.5">
          <summary className="cursor-pointer text-xs font-medium text-default">
            本机断联兜底 SMTP（可选，仅在与 Hub 断联时生效）
          </summary>
          <div className="mt-3 space-y-3">
            <Checkbox
              label="启用本机断联兜底邮件告警"
              checked={draft.enabled}
              onCheckedChange={(checked) => patchDraft({ enabled: checked })}
            />
            {smtpFields}
            <div className="flex justify-end">
              <Button
                type="button"
                color="secondary"
                variant="outline"
                size="sm"
                disabled={testMutation.isPending || saveMutation.isPending || !secretConfigured}
                onClick={() => testMutation.mutate({ localOnly: true })}
              >
                {testMutation.isPending && testingWorkerId === null ? '发送中…' : '测试本机兜底发信'}
              </Button>
            </div>
          </div>
        </details>
      ) : (
        <>
          <Checkbox
            label="启用关键异常邮件告警"
            checked={draft.enabled}
            onCheckedChange={(checked) => patchDraft({ enabled: checked })}
          />
          {smtpFields}
        </>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-subtle pt-3">
        <div className="min-w-0 flex-1">
          {feedback ? (
            <p className={`text-xs ${feedbackError ? 'text-danger' : 'text-secondary'}`} role="status" aria-live="polite">
              {feedback}
            </p>
          ) : dirty ? (
            <p className="text-2xs text-warning">有未保存的修改；测试邮件基于已保存配置发送。</p>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {dirty ? (
            <Button
              type="button"
              color="secondary"
              variant="ghost"
              size="sm"
              disabled={saveMutation.isPending}
              onClick={resetDraft}
            >
              放弃修改
            </Button>
          ) : null}
          <Button
            type="button"
            color="secondary"
            variant="outline"
            size="sm"
            disabled={
              testMutation.isPending ||
              saveMutation.isPending ||
              (isWorker ? !data?.hub_synced || !data?.hub_email_enabled : !secretConfigured)
            }
            onClick={() => testMutation.mutate(undefined)}
          >
            {testMutation.isPending && testingWorkerId === null
              ? '发送中…'
              : isWorker
                ? '经由 Hub 测试发信'
                : '发送测试邮件'}
          </Button>
          <Button
            type="button"
            color="primary"
            size="sm"
            disabled={saveMutation.isPending || invalidRecipients.length > 0 || invalidSender}
            onClick={() => saveMutation.mutate()}
          >
            {saveMutation.isPending ? '保存中…' : isWorker ? '保存并同步至 Hub' : '保存邮件配置'}
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
            {workerRecipients.map((item) => {
              const raw = workerDrafts[item.worker_id] ?? ''
              const rowBad = findInvalidEmails(parseRecipients(raw))
              const rowSaving = savingWorkerId === item.worker_id
              const rowTesting = testingWorkerId === item.worker_id
              return (
                <div key={item.worker_id} className="space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="w-28 shrink-0 truncate text-xs text-default">{item.worker_name}</span>
                    <div className="min-w-0 flex-1">
                      <Input
                        aria-label={`${item.worker_name} 专属收件邮箱`}
                        value={raw}
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
                      disabled={rowSaving || rowBad.length > 0}
                      onClick={() =>
                        saveWorkerRecipientsMutation.mutate({
                          workerId: item.worker_id,
                          raw,
                        })
                      }
                    >
                      {rowSaving ? '保存中…' : '保存'}
                    </Button>
                    <Button
                      type="button"
                      color="secondary"
                      variant="ghost"
                      size="sm"
                      disabled={rowTesting || !secretConfigured}
                      onClick={() => testMutation.mutate({ workerId: item.worker_id })}
                    >
                      {rowTesting ? '发送中…' : '测试'}
                    </Button>
                  </div>
                  {rowBad.length > 0 ? (
                    <p className="pl-30 text-2xs text-danger">邮箱格式无效：{rowBad.join('、')}</p>
                  ) : null}
                </div>
              )
            })}
          </div>
        </div>
      ) : null}

      {deliveries.length > 0 || (isHub && workerRecipients.length > 0) ? (
        <div className="space-y-2 border-t border-subtle pt-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs font-medium text-default">
              {isWorker ? '本机最近告警投递记录' : '最近告警投递记录（全局）'}
            </span>
            <div className="flex flex-wrap items-center gap-2">
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
              <Button
                type="button"
                color="secondary"
                variant="ghost"
                size="sm"
                disabled={query.isFetching}
                onClick={() => void query.refetch()}
              >
                {query.isFetching ? '刷新中…' : '刷新记录'}
              </Button>
            </div>
          </div>
          {deliveries.length === 0 ? (
            <p className="text-xs text-tertiary">暂无投递记录。</p>
          ) : (
            <div className="space-y-2">
              <ul className="space-y-1.5">
                {visibleDeliveries.map((item) => {
                  const meta = DELIVERY_STATUS_META[item.status] ?? { label: item.status, tone: 'secondary' as StatusTone }
                  const timeText = formatTime(item.sent_at || item.created_at)
                  const recips = item.recipients ?? []
                  return (
                    <li
                      key={item.id}
                      className="space-y-1 rounded-lg border border-subtle bg-surface-secondary px-3 py-2 text-xs"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex min-w-0 items-center gap-2">
                          <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
                          {isHub && item.device_name ? (
                            <span className="shrink-0 rounded bg-surface px-1.5 py-0.5 text-2xs text-secondary">{item.device_name}</span>
                          ) : null}
                          <span className="truncate text-default">{item.title}</span>
                        </div>
                        <span className="shrink-0 text-2xs text-tertiary">{timeText}</span>
                      </div>
                      {recips.length > 0 ? (
                        <p className="text-2xs text-tertiary">收件人：{recips.join('、')}</p>
                      ) : null}
                      {item.error ? (
                        <p className="text-2xs text-danger break-all">{item.error}</p>
                      ) : null}
                    </li>
                  )
                })}
              </ul>
              {deliveries.length > 8 ? (
                <Button
                  type="button"
                  color="secondary"
                  variant="ghost"
                  size="sm"
                  onClick={() => setShowAllDeliveries((prev) => !prev)}
                >
                  {showAllDeliveries ? '收起记录' : `展开全部 (${deliveries.length})`}
                </Button>
              ) : null}
            </div>
          )}
        </div>
      ) : null}
    </div>
  )
}
