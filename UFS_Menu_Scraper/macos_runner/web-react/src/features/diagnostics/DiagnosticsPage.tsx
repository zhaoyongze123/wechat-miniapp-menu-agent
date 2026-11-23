import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { useRole } from '../../app/RoleProvider'
import { formatTime } from '../hub/format'
import { connectConnections, fetchConnections, openEnvironmentSettings, runEnvironmentTest } from './api'
import { checkStatusMeta, connectionStatusMeta, settingsPaneOf, testTargetOf } from './format'
import type { CheckAction } from './types'

const CONNECTIONS_KEY = ['diagnostics', 'connections'] as const

/**
 * 环境与连接：只读快照 + 用户主动触发的真实检测。
 * 页面不轮询，避免后台反复启动桌面执行器；检测结果由 `/api/connections` 统一事实源提供。
 */
export function DiagnosticsPage() {
  const role = useRole()
  const toast = useToast()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: CONNECTIONS_KEY,
    queryFn: ({ signal }) => fetchConnections(signal),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  })

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: CONNECTIONS_KEY })
  }

  const connectMutation = useMutation({
    mutationFn: () => connectConnections(true),
    onSuccess: (data) => {
      queryClient.setQueryData(CONNECTIONS_KEY, data)
      toast.push(data.status === 'ready' ? '环境检查通过' : '环境检查完成，请查看下方提示', data.status === 'ready' ? 'success' : 'info')
    },
    onError: (cause) => toast.push(errorMessage(cause), 'danger'),
  })

  const testMutation = useMutation({
    mutationFn: runEnvironmentTest,
    onSuccess: () => {
      toast.push('检测已完成', 'success')
      refresh()
    },
    onError: (cause) => toast.push(errorMessage(cause), 'danger'),
  })

  const settingsMutation = useMutation({
    mutationFn: openEnvironmentSettings,
    onSuccess: (data) => {
      toast.push(data.message || '已打开系统设置，授权后请重新检测', 'info')
      refresh()
    },
    onError: (cause) => toast.push(errorMessage(cause), 'danger'),
  })

  const busy = connectMutation.isPending || testMutation.isPending || settingsMutation.isPending

  const handleAction = (action: CheckAction, contextId: string) => {
    switch (action.kind) {
      case 'reconnect':
        connectMutation.mutate()
        break
      case 'settings':
        navigate('/settings')
        break
      case 'open_settings': {
        const pane = settingsPaneOf(action.target)
        if (!pane) {
          toast.push('该设置入口暂不支持自动打开，请手动前往系统设置', 'info')
          return
        }
        settingsMutation.mutate(pane)
        break
      }
      case 'test': {
        const target = testTargetOf(action.target, contextId)
        if (!target) {
          toast.push('该检查项暂不支持手动测试', 'info')
          return
        }
        testMutation.mutate(target)
        break
      }
      default:
        toast.push('暂不支持该操作', 'info')
    }
  }

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
  if (role.role === 'hub') {
    return (
      <StatePanel
        variant="info"
        title="仅 Windows 采集端可做环境诊断"
        description="共享后台不直接控制微信与桌面自动化；请在已配对的采集端检查环境，或到“采集电脑”查看设备在线与分配状态。"
      />
    )
  }
  if (query.isPending) return <StatePanel variant="loading" title="正在读取连接状态…" />
  if (query.isError) {
    return (
      <StatePanel
        variant="error"
        title="连接状态读取失败"
        description={errorMessage(query.error)}
        action={
          <Button color="secondary" variant="outline" size="sm" onClick={refresh}>
            重试
          </Button>
        }
      />
    )
  }

  const snapshot = query.data
  const statusMeta = connectionStatusMeta(snapshot.status)
  const modules = snapshot.modules ?? []
  const diagnostics = snapshot.diagnostics
  const events = [...(diagnostics?.events ?? [])].reverse()
  const checks = diagnostics?.checks ?? []

  return (
    <section className="space-y-6" aria-labelledby="diagnostics-heading">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 id="diagnostics-heading" className="heading-md">
            环境与连接
          </h1>
          <p className="mt-1 text-sm text-secondary">
            页面不会自动轮询检测；点击“检查连接”时执行一轮微信、采集引擎和 AI 模型验证。
          </p>
        </div>
        <Button color="primary" size="sm" disabled={busy} onClick={() => connectMutation.mutate()}>
          {connectMutation.isPending ? '检测中…' : snapshot.busy ? '继续等待本轮检测' : snapshot.checked_at ? '重新检测' : '检查连接'}
        </Button>
      </header>

      <section className="rounded-xl border border-subtle p-4" aria-label="总体状态">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm text-default">{snapshot.summary || '尚未手动检查连接状态'}</p>
            <p className="mt-1 text-xs text-tertiary">
              最近检查：{snapshot.checked_at ? formatTime(snapshot.checked_at) : '尚无检测记录'}
            </p>
            {snapshot.preflight?.blockers?.length ? (
              <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-danger">
                {snapshot.preflight.blockers.map((blocker) => (
                  <li key={blocker}>{blocker}</li>
                ))}
              </ul>
            ) : null}
          </div>
          <StatusBadge tone={statusMeta.tone}>{statusMeta.label}</StatusBadge>
        </div>
      </section>

      <div className="grid gap-3 md:grid-cols-3" aria-live="polite">
        {modules.map((module) => {
          const meta = checkStatusMeta(module.status)
          return (
            <article key={module.id} className="space-y-2 rounded-xl border border-subtle p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="heading-xs">{module.name}</h2>
                <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
              </div>
              <p className="text-sm text-secondary">{module.summary}</p>
              <p className="text-2xs text-tertiary">检测时间：{formatTime(module.checked_at)}</p>
              {module.detail ? (
                <details className="text-2xs text-tertiary">
                  <summary className="cursor-pointer">查看详情</summary>
                  <p className="mt-1 whitespace-pre-wrap leading-relaxed">{module.detail}</p>
                </details>
              ) : null}
              <ActionButtons actions={module.actions ?? []} contextId={module.id} busy={busy} onAction={handleAction} />
            </article>
          )
        })}
        {modules.length === 0 ? <p className="text-sm text-secondary">暂无连接模块。</p> : null}
      </div>

      <details className="rounded-xl border border-subtle p-4">
        <summary className="cursor-pointer heading-xs">高级诊断 · 原始检查项与连接事件</summary>
        <p className="mt-2 text-xs text-tertiary">当前阶段：{diagnostics?.stage || '—'}</p>

        <div className="mt-3 space-y-1 rounded-lg bg-surface-secondary p-3">
          {events.length === 0 ? (
            <p className="text-xs text-tertiary">暂无连接事件</p>
          ) : (
            events.map((event, index) => (
              <p key={`${event.time}-${index}`} className="text-2xs text-secondary">
                <span className="text-tertiary">{formatTime(event.time)}</span>{' '}
                <span className="text-default">{event.stage || event.status || 'info'}</span> {event.message}
              </p>
            ))
          )}
        </div>

        <details className="mt-4">
          <summary className="cursor-pointer text-sm text-default">底层检查项（{checks.length}）</summary>
          <div className="mt-2 grid gap-2 md:grid-cols-2">
            {checks.map((check) => {
              const meta = checkStatusMeta(check.status)
              return (
                <article key={check.id} className="rounded-lg border border-subtle p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-sm text-default">{check.name || check.id}</span>
                    <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
                  </div>
                  <p className="mt-1 text-xs text-secondary">{check.summary}</p>
                  <p className="text-2xs text-tertiary">检测时间：{formatTime(check.checked_at)}</p>
                  <ActionButtons actions={check.actions ?? []} contextId={check.id} busy={busy} onAction={handleAction} />
                </article>
              )
            })}
          </div>
        </details>
      </details>
    </section>
  )
}

function ActionButtons({
  actions,
  contextId,
  busy,
  onAction,
}: {
  actions: CheckAction[]
  contextId: string
  busy: boolean
  onAction: (action: CheckAction, contextId: string) => void
}) {
  if (!actions.length) return null
  return (
    <div className="flex flex-wrap gap-2 pt-1">
      {actions.map((action, index) => (
        <Button
          key={`${action.kind}-${action.target ?? index}`}
          color="secondary"
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => onAction(action, contextId)}
        >
          {action.label}
        </Button>
      ))}
    </div>
  )
}
