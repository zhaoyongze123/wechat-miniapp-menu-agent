import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { CopyButton } from '../../components/CopyButton'
import { KeyValueList } from '../../components/KeyValueList'
import { PageHeader } from '../../components/PageHeader'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import {
  FormField,
  SettingRow,
  SettingSection,
  SettingsLayout,
  SettingsSaveBar,
  type SettingsNavItem,
} from '../../components/SettingsLayout'
import { useToast } from '../../components/ToastProvider'
import { useUnsavedChanges } from '../../components/UnsavedChangesProvider'
import { apiFetch, errorMessage } from '../../api/client'
import { ROLE_LABEL, useRole } from '../../app/RoleProvider'
import { formatTime } from '../hub/format'
import {
  clearDebugLogs,
  saveConfig,
  selectDirectory,
  testModelConnection,
} from './api'
import {
  debugUsageText,
  formFromConfig,
  payloadFromForm,
  validateForm,
  type SettingsFormState,
} from './format'
import { settingsKeys, useConfig, useDebugSummary } from './hooks'
import { SharedWorkbenchCard } from './SharedWorkbenchCard'
import { ModelUsageDialog } from '../model-usage/ModelUsageDialog'
import { NotificationPreferencesPanel } from '../notifications/NotificationPreferencesPanel'
import { UpdatesPanel } from '../updates/UpdatesPanel'
import { useUpdateStatus } from '../updates/hooks'
import type { DirectoryKind, ModelProvider, ModelTestResult, ThinkingMode } from './types'

type CategoryId = 'general' | 'notifications' | 'connection' | 'model' | 'capture' | 'storage' | 'updates' | 'about'

const BASE_CATEGORIES: SettingsNavItem[] = [
  { id: 'general', label: '常规', description: '目录与基础偏好' },
  { id: 'notifications', label: '通知与提醒', description: '声音与桌面提醒' },
  { id: 'connection', label: '设备与连接', description: '共享后台与配对' },
  { id: 'model', label: '模型服务', description: '接口、密钥与测试' },
  { id: 'capture', label: '采集配置', description: '页数、步数与时限' },
  { id: 'storage', label: '存储与日志', description: '容量与清理' },
  { id: 'updates', label: '软件更新', description: '版本发现与清点' },
  { id: 'about', label: '关于', description: '版本与诊断入口' },
]

const CONFIG_CATEGORIES = new Set<CategoryId>(['general', 'model', 'capture', 'storage'])

/**
 * 设置页：配置事实源始终是服务端 `/api/config`。
 * 表单只在首次加载与保存成功时同步；后台刷新不会覆盖正在编辑的草稿。
 */
export function SettingsPage() {
  const role = useRole()
  const toast = useToast()
  const queryClient = useQueryClient()
  const configQuery = useConfig()
  const [active, setActive] = useState<CategoryId>('general')
  const debugQuery = useDebugSummary(active === 'storage')
  const versionQuery = useQuery({
    queryKey: ['service-version'],
    queryFn: ({ signal }) => apiFetch<{ version: string; frontend: string }>('/api/version', { signal }),
    enabled: active === 'about',
    staleTime: 60_000,
  })
  const updateStatusQuery = useUpdateStatus()
  const [form, setForm] = useState<SettingsFormState | null>(null)
  const [baseline, setBaseline] = useState<SettingsFormState | null>(null)
  const [error, setError] = useState('')
  const [testResult, setTestResult] = useState<ModelTestResult | null>(null)
  const [clearOpen, setClearOpen] = useState(false)
  const [usageOpen, setUsageOpen] = useState(false)

  useEffect(() => {
    if (!configQuery.data || baseline) return
    const next = formFromConfig(configQuery.data)
    setBaseline(next)
    setForm(next)
  }, [baseline, configQuery.data])

  const dirty = Boolean(form && baseline && JSON.stringify(form) !== JSON.stringify(baseline))
  useUnsavedChanges(dirty, '设置中有未保存的修改，离开后这些修改将丢失。')

  const patch = (partial: Partial<SettingsFormState>) => {
    setForm((current) => (current ? { ...current, ...partial } : current))
  }

  const saveMutation = useMutation({
    mutationFn: saveConfig,
    onSuccess: (data) => {
      // 保存响应即最新事实源；同步缓存与基线，后续后台刷新不再覆盖。
      queryClient.setQueryData(settingsKeys.config, data)
      const next = formFromConfig(data)
      setForm(next)
      setBaseline(next)
      setError('')
      setTestResult(null)
      toast.push('配置已保存', 'success')
    },
    onError: (cause) => setError(errorMessage(cause)),
  })

  const pickMutation = useMutation({
    mutationFn: selectDirectory,
    onSuccess: (data, kind) => {
      if (data.cancelled || !data.path) return
      setForm((current) =>
        current ? { ...current, ...(kind === 'output' ? { output_dir: data.path ?? '' } : { input_dir: data.path ?? '' }) } : current,
      )
    },
    onError: (cause) => toast.push(errorMessage(cause), 'danger'),
  })

  const testMutation = useMutation({
    mutationFn: testModelConnection,
    onSuccess: (data) => {
      setTestResult(data)
      toast.push(data.ok ? '模型连接正常' : '模型连接未通过', data.ok ? 'success' : 'danger')
    },
    onError: (cause) => {
      setTestResult(null)
      toast.push(errorMessage(cause), 'danger')
    },
  })

  const clearMutation = useMutation({
    mutationFn: clearDebugLogs,
    onSuccess: () => {
      setClearOpen(false)
      void debugQuery.refetch()
      toast.push('已清理调试日志与调试截图', 'success')
    },
    onError: (cause) => toast.push(errorMessage(cause), 'danger'),
  })

  if (role.loading) return <StatePanel variant="loading" title="正在确认当前角色…" />
  if (role.error) {
    return (
      <StatePanel
        variant="error"
        title="无法确认当前服务角色"
        description={role.error}
        action={
          <Button color="secondary" variant="outline" size="sm" onClick={role.reload}>
            重试
          </Button>
        }
      />
    )
  }
  if (!form) {
    if (configQuery.isError) {
      return (
        <StatePanel
          variant="error"
          title="配置读取失败"
          description={errorMessage(configQuery.error)}
          action={
            <Button color="secondary" variant="outline" size="sm" onClick={() => void configQuery.refetch()}>
              重试
            </Button>
          }
        />
      )
    }
    return <StatePanel variant="loading" title="正在读取本机配置…" />
  }

  const isHub = role.role === 'hub'
  const canSelectDirectory = !isHub
  const modelConfigured = Boolean(configQuery.data?.model.base_url && configQuery.data?.model.api_key_configured)
  const debug = debugQuery.data
  const categories = BASE_CATEGORIES
    .filter((item) => item.id !== 'capture' || !isHub)
    .map((item) =>
      item.id === 'updates' && updateStatusQuery.data?.update_available
        ? { ...item, badge: '有新版本' }
        : item,
    )

  const save = () => {
    const message = validateForm(form)
    if (message) {
      setError(message)
      return
    }
    setError('')
    saveMutation.mutate(payloadFromForm(form))
  }

  const reset = () => {
    if (!baseline) return
    setForm(baseline)
    setError('')
    setTestResult(null)
  }

  return (
    <section className="space-y-6" aria-labelledby="settings-heading">
      <PageHeader title="设置" headingId="settings-heading" />

      <SettingsLayout items={categories} activeId={active} onSelect={(id) => setActive(id as CategoryId)}>
        {CONFIG_CATEGORIES.has(active) ? (
          <form
            className="space-y-6"
            onSubmit={(event) => {
              event.preventDefault()
              save()
            }}
          >
            {active === 'general' ? (
              <SettingSection id="general" title="目录" description="目录属于本机配置；共享后台与采集端的路径含义不同。">
                {role.role === 'worker' ? (
                  <div className="rounded-lg border border-subtle bg-surface-secondary px-3 py-2">
                    <p className="text-sm text-default">任务来源：由共享工作台分配</p>
                    <p className="mt-1 text-2xs text-tertiary">
                      采集端不接受本机导入任务；素材、分配与发布在共享后台完成，结果仍写入本机输出目录。
                    </p>
                  </div>
                ) : (
                  <DirectoryField
                    label="输入目录"
                    value={form.input_dir}
                    canPick={canSelectDirectory}
                    disabled={pickMutation.isPending}
                    note={isHub ? '共享后台不执行本机采集，目录由已配对采集端维护。' : undefined}
                    onPick={() => pickMutation.mutate('input' as DirectoryKind)}
                    busy={pickMutation.isPending && pickMutation.variables === 'input'}
                  />
                )}
                <DirectoryField
                  label="输出目录"
                  value={form.output_dir}
                  canPick={canSelectDirectory}
                  disabled={pickMutation.isPending}
                  note={isHub ? '共享后台保存服务端结果与素材的路径。' : '采集截图与结果文件写入此目录；请选择本机可写路径。'}
                  onPick={() => pickMutation.mutate('output')}
                  busy={pickMutation.isPending && pickMutation.variables === 'output'}
                />
              </SettingSection>
            ) : null}

            {active === 'model' ? (
              <SettingSection id="model" title="模型服务" description="用于采集后的菜单识别与素材分析；密钥只在服务端保存。">
                <div className="grid gap-3 sm:grid-cols-2">
                  <FormField label="供应商">
                    <Select
                      aria-label="模型供应商"
                      value={form.provider}
                      options={[
                        { value: 'openai_compatible', label: 'OpenAI 兼容接口' },
                        { value: 'anthropic', label: 'Anthropic' },
                      ]}
                      onChange={(option) => patch({ provider: option.value as ModelProvider })}
                    />
                  </FormField>
                  <FormField label="模型名称" htmlFor="settings-model">
                    <Input
                      id="settings-model"
                      value={form.model}
                      placeholder="例如：模型部署名称"
                      onChange={(event) => patch({ model: event.target.value })}
                    />
                  </FormField>
                  <FormField label="接口地址" htmlFor="settings-base-url" full>
                    <Input
                      id="settings-base-url"
                      value={form.base_url}
                      placeholder="https://…"
                      autoComplete="url"
                      onChange={(event) => patch({ base_url: event.target.value })}
                    />
                  </FormField>
                  <FormField
                    label="API 密钥"
                    htmlFor="settings-api-key"
                    full
                    help={
                      form.clear_api_key
                        ? '保存后将清除已保存的密钥。'
                        : configQuery.data?.model.api_key_configured
                          ? '已保存密钥；留空表示保留。服务端不会在读取配置时返回密钥。'
                          : '尚未配置密钥；填写后保存。'
                    }
                  >
                    <Input
                      id="settings-api-key"
                      type="password"
                      value={form.api_key}
                      placeholder={configQuery.data?.model.api_key_configured ? '留空表示保留已保存的密钥' : '输入 API 密钥'}
                      autoComplete="new-password"
                      onChange={(event) => patch({ api_key: event.target.value })}
                    />
                  </FormField>
                  <div className="sm:col-span-2">
                    <Checkbox
                      label="清除已保存的密钥"
                      checked={form.clear_api_key}
                      onCheckedChange={(next) => patch({ clear_api_key: next })}
                    />
                  </div>
                  <FormField label="模型思考模式">
                    <Select
                      aria-label="模型思考模式"
                      value={form.enable_thinking}
                      options={[
                        { value: 'auto', label: '服务默认' },
                        { value: 'off', label: '关闭（支持该参数的兼容接口）' },
                        { value: 'on', label: '开启' },
                      ]}
                      onChange={(option) => patch({ enable_thinking: option.value as ThinkingMode })}
                    />
                  </FormField>
                  <FormField label="请求超时（秒）" htmlFor="settings-timeout" help="10–300 秒。">
                    <Input
                      id="settings-timeout"
                      type="number"
                      min={10}
                      max={300}
                      step={1}
                      value={form.timeout_seconds}
                      onChange={(event) => patch({ timeout_seconds: event.target.value })}
                    />
                  </FormField>
                </div>

                <div className="space-y-3 rounded-lg border border-subtle bg-surface-secondary p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm text-default">连接测试</span>
                    <StatusBadge tone={modelConfigured ? 'success' : 'warning'}>
                      {modelConfigured ? '已配置' : '待配置'}
                    </StatusBadge>
                    <span className="text-xs text-secondary">
                      {modelConfigured ? '地址与密钥已保存' : '先保存供应商、地址和密钥'}
                    </span>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      type="button"
                      color="secondary"
                      variant="outline"
                      size="sm"
                      disabled={!modelConfigured || testMutation.isPending}
                      onClick={() => testMutation.mutate()}
                    >
                      {testMutation.isPending ? '测试中…' : '测试模型连接'}
                    </Button>
                    <Button type="button" color="secondary" variant="outline" size="sm" onClick={() => setUsageOpen(true)}>
                      查看模型用量
                    </Button>
                  </div>
                  <p className="text-2xs text-tertiary">测试使用已保存配置；当前未保存的修改不会参与。</p>
                  {testResult ? (
                    <Alert
                      color={testResult.ok ? 'success' : 'danger'}
                      variant="soft"
                      title={testResult.ok ? '模型连接正常' : '模型连接未通过'}
                      description={`${testResult.message}${testResult.checked_at ? ` · ${formatTime(testResult.checked_at)}` : ''}`}
                    />
                  ) : null}
                </div>
              </SettingSection>
            ) : null}

            {active === 'capture' && !isHub ? (
              <SettingSection id="capture" title="采集配置" description="仅影响后续任务；采集或标注运行中时服务端会拒绝保存。">
                <div className="grid gap-3 sm:grid-cols-2">
                  <FormField label="就餐人数" htmlFor="settings-people" help="小程序要求选择人数时使用；采集始终逐个执行。">
                    <Input
                      id="settings-people"
                      type="number"
                      min={1}
                      max={12}
                      step={1}
                      value={form.people_count}
                      onChange={(event) => patch({ people_count: event.target.value })}
                    />
                  </FormField>
                  <FormField label="最大页数" htmlFor="settings-max-pages" help="2–500。">
                    <Input
                      id="settings-max-pages"
                      type="number"
                      min={2}
                      max={500}
                      value={form.max_pages}
                      onChange={(event) => patch({ max_pages: event.target.value })}
                    />
                  </FormField>
                  <FormField label="最大 Agent 步数" htmlFor="settings-max-steps" help="2–60。">
                    <Input
                      id="settings-max-steps"
                      type="number"
                      min={2}
                      max={60}
                      value={form.max_agent_steps}
                      onChange={(event) => patch({ max_agent_steps: event.target.value })}
                    />
                  </FormField>
                  <FormField label="单任务上限（秒）" htmlFor="settings-max-seconds" help="60–7200。">
                    <Input
                      id="settings-max-seconds"
                      type="number"
                      min={60}
                      max={7200}
                      value={form.max_task_seconds}
                      onChange={(event) => patch({ max_task_seconds: event.target.value })}
                    />
                  </FormField>
                  <FormField label="页面稳定等待（秒）" htmlFor="settings-settle" help="0.3–10。">
                    <Input
                      id="settings-settle"
                      type="number"
                      min={0.3}
                      max={10}
                      step={0.1}
                      value={form.settle_seconds}
                      onChange={(event) => patch({ settle_seconds: event.target.value })}
                    />
                  </FormField>
                  <FormField label="素材分析并发" htmlFor="settings-concurrency" help="1–4；全局仍由任务队列单通道执行。">
                    <Input
                      id="settings-concurrency"
                      type="number"
                      min={1}
                      max={4}
                      value={form.analysis_concurrency}
                      onChange={(event) => patch({ analysis_concurrency: event.target.value })}
                    />
                  </FormField>
                </div>

                <div className="space-y-2 border-t border-subtle pt-3">
                  <Checkbox
                    label="人工标注时提供授权操作建议"
                    checked={form.auto_authorize}
                    onCheckedChange={(next) => patch({ auto_authorize: next })}
                  />
                  <p className="text-2xs text-tertiary">
                    仅用于人工标注建议；正式采集的导航与终态由 GUI Agent 按业务经验决定。
                  </p>
                </div>

                <details className="rounded-lg border border-subtle px-3 py-2">
                  <summary className="cursor-pointer text-xs text-secondary">高级：运行时自动控制的参数</summary>
                  <div className="mt-3 grid gap-3 sm:grid-cols-2">
                    <FormField label="目标窗口宽度（只读）" help="运行时自动调整，仅展示当前保存值。">
                      <Input type="number" value={form.window_width} disabled readOnly />
                    </FormField>
                    <FormField label="目标窗口高度（只读）">
                      <Input type="number" value={form.window_height} disabled readOnly />
                    </FormField>
                    <FormField label="重叠比例（只读）" help="运行时自动调整；滚动距离使用总览中的“滚动步数”。">
                      <Input type="number" value={form.overlap_ratio} disabled readOnly />
                    </FormField>
                  </div>
                </details>
              </SettingSection>
            ) : null}

            {active === 'storage' ? (
              <SettingSection id="storage" title="存储与日志" description="仅清理调试日志与调试截图，不影响门店素材、正式结果与数据库。">
                <div className="grid gap-3 sm:grid-cols-2">
                  <FormField
                    label="最大存储空间（MB）"
                    htmlFor="settings-max-storage"
                    help="64–4096；达到上限后自动清理较早记录。"
                  >
                    <Input
                      id="settings-max-storage"
                      type="number"
                      min={64}
                      max={4096}
                      step={64}
                      value={form.max_storage_mb}
                      onChange={(event) => patch({ max_storage_mb: event.target.value })}
                    />
                  </FormField>
                  <div className="space-y-1">
                    <span className="block text-sm text-default">当前占用</span>
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-xs text-secondary">
                        {debugQuery.isPending
                          ? '正在读取占用…'
                          : debugQuery.isError
                            ? errorMessage(debugQuery.error)
                            : debug
                              ? debugUsageText(debug)
                              : '尚未读取占用'}
                      </span>
                      <Button type="button" color="secondary" variant="outline" size="sm" onClick={() => void debugQuery.refetch()}>
                        刷新占用
                      </Button>
                      <Button type="button" color="danger" variant="outline" size="sm" onClick={() => setClearOpen(true)}>
                        清理旧日志
                      </Button>
                    </div>
                    {debug && (debug.dropped_records > 0 || debug.dropped_evidence > 0) ? (
                      <p className="text-2xs text-tertiary">
                        已因容量上限丢弃 {debug.dropped_records} 条记录、{debug.dropped_evidence} 份证据。
                      </p>
                    ) : null}
                  </div>
                </div>
              </SettingSection>
            ) : null}

            <SettingsSaveBar
              dirty={dirty}
              saving={saveMutation.isPending}
              error={error}
              note="保存后对后续任务生效；采集或标注运行中时服务端会拒绝修改。"
              onReset={reset}
            />
          </form>
        ) : null}

        {active === 'notifications' ? (
          <SettingSection id="notifications" title="通知与提醒" description="浏览器本地偏好，不影响 Windows 执行配置或共享后台。">
            <NotificationPreferencesPanel role={role.role} />
          </SettingSection>
        ) : null}

        {active === 'connection' ? (
          <SettingSection id="connection" title="设备与连接" description="共享后台地址、配对状态与待补传结果。">
            <SharedWorkbenchCard />
          </SettingSection>
        ) : null}

        {active === 'updates' ? <UpdatesPanel /> : null}

        {active === 'about' ? (
          <SettingSection id="about" title="关于" description="版本、运行角色与维护入口均来自真实接口。">
            <KeyValueList
              items={[
                { label: '运行角色', value: ROLE_LABEL[role.role] },
                {
                  label: '服务版本',
                  value: versionQuery.isPending
                    ? '正在读取…'
                    : versionQuery.isError
                      ? errorMessage(versionQuery.error)
                      : versionQuery.data?.version || '未知',
                  ...(versionQuery.data?.version ? { copy: versionQuery.data.version } : {}),
                },
                {
                  label: '前端构建',
                  value: versionQuery.data?.frontend || '未知',
                  ...(versionQuery.data?.frontend ? { copy: versionQuery.data.frontend, mono: true } : {}),
                },
                {
                  label: '共享状态',
                  value: role.status?.role === 'hub'
                    ? `已配对 ${role.status.workers.length} 台采集电脑`
                    : role.status?.role === 'worker'
                      ? role.status.paired
                        ? role.status.online
                          ? '已连接共享后台'
                          : '共享后台离线'
                        : '尚未配对'
                      : '未启用共享角色',
                },
              ]}
            />
            <div className="flex flex-wrap items-center gap-2 border-t border-subtle pt-3">
              <Link
                to="/diagnostics"
                className="rounded-md border border-subtle px-3 py-1.5 text-sm text-default hover:bg-secondary-ghost-hover"
              >
                环境诊断
              </Link>
              {!isHub ? (
                <Link
                  to="/annotation"
                  className="rounded-md border border-subtle px-3 py-1.5 text-sm text-default hover:bg-secondary-ghost-hover"
                >
                  人工标注
                </Link>
              ) : null}
              <Button type="button" color="secondary" variant="ghost" size="sm" onClick={() => void versionQuery.refetch()}>
                刷新版本
              </Button>
            </div>
            <p className="text-2xs text-tertiary">
              当前不提供自动更新；版本升级由维护者在确认采集空闲后执行。
            </p>
          </SettingSection>
        ) : null}
      </SettingsLayout>

      <ConfirmActionDialog
        open={clearOpen}
        title="清理调试日志"
        description={
          <div className="space-y-1">
            <p>将删除已保存的调试日志和调试截图。</p>
            <p>门店素材、菜单截图与采集结果会保留。</p>
          </div>
        }
        confirmLabel="确认清理"
        tone="danger"
        busy={clearMutation.isPending}
        onOpenChange={(next) => {
          if (!next && !clearMutation.isPending) setClearOpen(false)
        }}
        onConfirm={() => clearMutation.mutate()}
      />

      <ModelUsageDialog open={usageOpen} onOpenChange={setUsageOpen} />
    </section>
  )
}

function DirectoryField({
  label,
  value,
  canPick,
  disabled,
  note,
  busy,
  onPick,
}: {
  label: string
  value: string
  /** Hub 不执行本机采集，直接隐藏目录选择入口而不是展示一个禁用按钮。 */
  canPick: boolean
  disabled: boolean
  note?: string | undefined
  busy: boolean
  onPick: () => void
}) {
  const id = `directory-${label === '输入目录' ? 'input' : 'output'}`
  return (
    <SettingRow label={label} htmlFor={id} {...(note ? { description: note } : {})} wide>
      <div className="flex gap-2">
        <div className="min-w-0 flex-1">
          <Input id={id} value={value} readOnly placeholder="尚未选择" aria-label={label} title={value} />
        </div>
        {value ? <CopyButton value={value} label={label} /> : null}
        {canPick ? (
          <Button type="button" color="secondary" variant="outline" size="sm" disabled={disabled} onClick={onPick}>
            {busy ? '选择中…' : '选择目录'}
          </Button>
        ) : null}
      </div>
    </SettingRow>
  )
}
