import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { PageHeader } from '../../components/PageHeader'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { useRole } from '../../app/RoleProvider'
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
import type { DirectoryKind, ModelProvider, ModelTestResult, ThinkingMode } from './types'

/**
 * 设置页：配置事实源始终是服务端 `/api/config`。
 * 表单只在首次加载与保存成功时同步，避免后台刷新覆盖用户正在编辑的内容。
 */
export function SettingsPage() {
  const role = useRole()
  const toast = useToast()
  const queryClient = useQueryClient()
  const configQuery = useConfig()
  const debugQuery = useDebugSummary()
  const [form, setForm] = useState<SettingsFormState | null>(null)
  const [error, setError] = useState('')
  const [testResult, setTestResult] = useState<ModelTestResult | null>(null)
  const [clearOpen, setClearOpen] = useState(false)
  const [usageOpen, setUsageOpen] = useState(false)

  useEffect(() => {
    if (configQuery.data) setForm(formFromConfig(configQuery.data))
  }, [configQuery.data])

  const patch = (partial: Partial<SettingsFormState>) => {
    setForm((current) => (current ? { ...current, ...partial } : current))
  }

  const saveMutation = useMutation({
    mutationFn: saveConfig,
    onSuccess: (data) => {
      // 保存响应即最新事实源；同步缓存，模型“已配置”状态与目录展示立刻更新。
      queryClient.setQueryData(settingsKeys.config, data)
      setForm(formFromConfig(data))
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
  const isWorker = role.role === 'worker'
  const canSelectDirectory = !isHub
  const modelConfigured = Boolean(configQuery.data?.model.base_url && configQuery.data?.model.api_key_configured)
  const debug = debugQuery.data
  const dirtyTest = testResult !== null

  const save = () => {
    const message = validateForm(form)
    if (message) {
      setError(message)
      return
    }
    setError('')
    saveMutation.mutate(payloadFromForm(form))
  }

  return (
    <section className="space-y-6" aria-labelledby="settings-heading">
      <PageHeader title="设置" headingId="settings-heading" />

      <NotificationPreferencesPanel />
      <SharedWorkbenchCard />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
        <form
          className="space-y-5 rounded-xl border border-subtle p-4"
          onSubmit={(event) => {
            event.preventDefault()
            save()
          }}
        >
          <FormSection title="目录">
            {isWorker ? (
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
              note={isHub ? undefined : '采集截图与结果文件写入此目录；请选择本机可写路径。'}
              onPick={() => pickMutation.mutate('output')}
              busy={pickMutation.isPending && pickMutation.variables === 'output'}
            />
          </FormSection>

          <FormSection title="模型服务">
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="供应商">
                <Select
                  aria-label="模型供应商"
                  value={form.provider}
                  options={[
                    { value: 'openai_compatible', label: 'OpenAI 兼容接口' },
                    { value: 'anthropic', label: 'Anthropic' },
                  ]}
                  onChange={(option) => patch({ provider: option.value as ModelProvider })}
                />
              </Field>
              <Field label="模型名称">
                <Input
                  value={form.model}
                  placeholder="例如：模型部署名称"
                  onChange={(event) => patch({ model: event.target.value })}
                />
              </Field>
              <Field label="接口地址" full>
                <Input
                  value={form.base_url}
                  placeholder="https://…"
                  autoComplete="url"
                  onChange={(event) => patch({ base_url: event.target.value })}
                />
              </Field>
              <Field
                label="API 密钥"
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
                  type="password"
                  value={form.api_key}
                  placeholder={configQuery.data?.model.api_key_configured ? '留空表示保留已保存的密钥' : '输入 API 密钥'}
                  autoComplete="new-password"
                  onChange={(event) => patch({ api_key: event.target.value })}
                />
              </Field>
              <div className="sm:col-span-2">
                <Checkbox
                  label="清除已保存的密钥"
                  checked={form.clear_api_key}
                  onCheckedChange={(next) => patch({ clear_api_key: next })}
                />
              </div>
              <Field label="模型思考模式">
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
              </Field>
              <Field label="请求超时（秒）" help="10–300 秒。">
                <Input
                  type="number"
                  min={10}
                  max={300}
                  step={1}
                  value={form.timeout_seconds}
                  onChange={(event) => patch({ timeout_seconds: event.target.value })}
                />
              </Field>
              {!isHub ? (
                <>
                  <Field label="就餐人数" help="小程序要求选择人数时使用；采集始终逐个执行。">
                    <Input
                      type="number"
                      min={1}
                      max={12}
                      step={1}
                      value={form.people_count}
                      onChange={(event) => patch({ people_count: event.target.value })}
                    />
                  </Field>
                  <div className="sm:col-span-2">
                    <Checkbox
                      label="人工标注时提供授权操作建议"
                      checked={form.auto_authorize}
                      onCheckedChange={(next) => patch({ auto_authorize: next })}
                    />
                    <p className="mt-1 text-2xs text-tertiary">
                      仅用于人工标注建议；正式采集的导航与终态由 GUI Agent 按业务经验决定。
                    </p>
                  </div>
                </>
              ) : null}
            </div>
          </FormSection>

          {!isHub ? (
            <FormSection title="截图与循环参数">
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="目标窗口宽度（预留）" help="运行时自动调整，仅展示当前保存值。">
                  <Input type="number" value={form.window_width} disabled />
                </Field>
                <Field label="目标窗口高度（预留）">
                  <Input type="number" value={form.window_height} disabled />
                </Field>
                <Field label="重叠比例（预留）" help="运行时自动调整；滚动距离使用总览中的“滚动步数”。">
                  <Input type="number" value={form.overlap_ratio} disabled />
                </Field>
                <Field label="最大页数" help="2–500。">
                  <Input
                    type="number"
                    min={2}
                    max={500}
                    value={form.max_pages}
                    onChange={(event) => patch({ max_pages: event.target.value })}
                  />
                </Field>
                <Field label="最大 Agent 步数" help="2–60。">
                  <Input
                    type="number"
                    min={2}
                    max={60}
                    value={form.max_agent_steps}
                    onChange={(event) => patch({ max_agent_steps: event.target.value })}
                  />
                </Field>
                <Field label="单任务上限（秒）" help="60–7200。">
                  <Input
                    type="number"
                    min={60}
                    max={7200}
                    value={form.max_task_seconds}
                    onChange={(event) => patch({ max_task_seconds: event.target.value })}
                  />
                </Field>
                <Field label="页面稳定等待（秒）" help="0.3–10。">
                  <Input
                    type="number"
                    min={0.3}
                    max={10}
                    step={0.1}
                    value={form.settle_seconds}
                    onChange={(event) => patch({ settle_seconds: event.target.value })}
                  />
                </Field>
                <Field label="素材分析并发" help="1–4；全局仍由任务队列单通道执行。">
                  <Input
                    type="number"
                    min={1}
                    max={4}
                    value={form.analysis_concurrency}
                    onChange={(event) => patch({ analysis_concurrency: event.target.value })}
                  />
                </Field>
              </div>
            </FormSection>
          ) : null}

          <FormSection title="开发调试日志">
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="最大存储空间（MB）" help="64–4096；达到上限后自动清理较早记录，不影响门店素材与截图。">
                <Input
                  type="number"
                  min={64}
                  max={4096}
                  step={64}
                  value={form.max_storage_mb}
                  onChange={(event) => patch({ max_storage_mb: event.target.value })}
                />
              </Field>
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
                  <Button color="secondary" variant="outline" size="sm" onClick={() => void debugQuery.refetch()}>
                    刷新占用
                  </Button>
                  <Button color="danger" variant="outline" size="sm" onClick={() => setClearOpen(true)}>
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
          </FormSection>

          <div className="flex flex-wrap items-center gap-3 border-t border-subtle pt-4">
            <Button type="submit" color="primary" size="sm" disabled={saveMutation.isPending}>
              {saveMutation.isPending ? '保存中…' : '保存配置'}
            </Button>
            {error ? (
              <span className="text-xs text-danger" role="alert">
                {error}
              </span>
            ) : (
              <span className="text-xs text-tertiary">保存后对后续任务生效；运行中的任务不受影响。</span>
            )}
          </div>
        </form>

        <div className="space-y-4">
          <section className="rounded-xl border border-subtle p-4" aria-labelledby="model-test-heading">
            <h2 id="model-test-heading" className="heading-xs">
              模型连接
            </h2>
            <div className="mt-3 flex items-center gap-2">
              <StatusBadge tone={modelConfigured ? 'success' : 'warning'}>
                {modelConfigured ? '已配置' : '待配置'}
              </StatusBadge>
              <span className="text-xs text-secondary">
                {modelConfigured ? '地址与密钥已保存' : '先保存供应商、地址和密钥'}
              </span>
            </div>
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              className="mt-3 w-full"
              disabled={!modelConfigured || testMutation.isPending}
              onClick={() => testMutation.mutate()}
            >
              {testMutation.isPending ? '测试中…' : '测试模型连接'}
            </Button>
            <p className="mt-1 text-2xs text-tertiary">测试使用已保存配置，未保存的修改不会参与。</p>
            {dirtyTest && testResult ? (
              <Alert
                className="mt-3"
                color={testResult.ok ? 'success' : 'danger'}
                variant="soft"
                title={testResult.ok ? '模型连接正常' : '模型连接未通过'}
                description={`${testResult.message}${testResult.checked_at ? ` · ${formatTime(testResult.checked_at)}` : ''}`}
              />
            ) : null}
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              className="mt-2 w-full"
              onClick={() => setUsageOpen(true)}
            >
              查看模型用量
            </Button>
          </section>

          {!isHub ? (
            <section className="rounded-xl border border-subtle p-4" aria-labelledby="advanced-tools-heading">
              <h2 id="advanced-tools-heading" className="heading-xs">
                高级工具
              </h2>
              <div className="mt-3 space-y-2">
                <Link
                  to="/annotation"
                  className="block rounded-md border border-subtle px-3 py-1.5 text-center text-sm text-default hover:bg-secondary-ghost-hover"
                >
                  人工标注
                </Link>
                <Link
                  to="/diagnostics"
                  className="block rounded-md border border-subtle px-3 py-1.5 text-center text-sm text-default hover:bg-secondary-ghost-hover"
                >
                  环境诊断
                </Link>
              </div>
            </section>
          ) : null}

          {!isHub ? (
            <section className="rounded-xl border border-subtle p-4" aria-labelledby="capture-boundary-heading">
              <h2 id="capture-boundary-heading" className="heading-xs">
                采集边界
              </h2>
              <p className="mt-2 text-xs leading-relaxed text-secondary">
                本工具一次运行一个任务。暂停请求在当前任务边界和循环检查点处理；不会通过重复触发操作来模拟暂停。完成后请检查截图覆盖率。
              </p>
            </section>
          ) : null}
        </div>
      </div>

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

function FormSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-3" aria-label={title}>
      <h2 className="heading-xs">{title}</h2>
      {children}
    </section>
  )
}

function Field({
  label,
  help,
  full = false,
  children,
}: {
  label: string
  help?: string
  full?: boolean
  children: ReactNode
}) {
  return (
    <div className={full ? 'space-y-1 sm:col-span-2' : 'space-y-1'}>
      <span className="block text-sm text-default">{label}</span>
      {children}
      {help ? <p className="text-2xs text-tertiary">{help}</p> : null}
    </div>
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
  return (
    <div className="space-y-1">
      <span className="block text-sm text-default">{label}</span>
      <div className="flex gap-2">
        <div className="min-w-0 flex-1">
          <Input value={value} readOnly placeholder="尚未选择" aria-label={label} />
        </div>
        {canPick ? (
          <Button color="secondary" variant="outline" size="sm" disabled={disabled} onClick={onPick}>
            {busy ? '选择中…' : '选择目录'}
          </Button>
        ) : null}
      </div>
      {note ? <p className="text-2xs text-tertiary">{note}</p> : null}
    </div>
  )
}
