import { Button } from '@openai/apps-sdk-ui/components/Button'
import { useEffect, useState, useSyncExternalStore } from 'react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { StatusBadge } from '../../components/StatusBadge'
import { useRole } from '../../app/RoleProvider'
import { useUpdateStatus } from './hooks'
import type { UpdateFlowStage } from './types'
import {
  clearActiveUpdateSession,
  readActiveUpdateSession,
  subscribeActiveUpdateSession,
  writeActiveUpdateSession,
  type ActiveUpdateSession,
} from './updateSession'

const STAGE_LABEL: Record<UpdateFlowStage, string> = {
  idle: '准备更新',
  downloading: '正在下载并校验更新包',
  waiting: '等待当前任务结束后安装',
  installing: '正在安装并替换程序',
  verifying: '正在验证新版本健康状态',
  restarting: '正在重启服务',
  rolling_back: '更新异常，正在自动回滚',
  completed: '更新完成',
  failed: '更新失败',
  rolled_back: '已回滚到上一版本',
}

const STEP_LIST = [
  { key: 'prepare', label: '准备与下载' },
  { key: 'install', label: '安装与替换' },
  { key: 'restart', label: '重启与验证' },
  { key: 'reconnect', label: '恢复连接' },
] as const

function stageToStepIndex(stage: UpdateFlowStage, disconnected: boolean): number {
  if (stage === 'completed') return 3
  if (disconnected) return 2
  if (stage === 'downloading' || stage === 'waiting' || stage === 'idle') return 0
  if (stage === 'installing') return 1
  if (stage === 'verifying' || stage === 'restarting' || stage === 'rolling_back') return 2
  return 1
}

function formatElapsed(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes === 0) return `${seconds} 秒`
  return `${minutes} 分 ${seconds} 秒`
}

let cachedSnapshotRaw: string | null = null
let cachedSnapshotValue: ActiveUpdateSession | null = null

function getSessionSnapshot(): ActiveUpdateSession | null {
  try {
    const raw = window.localStorage.getItem('menu-capture.active-update-session')
    if (raw === cachedSnapshotRaw) return cachedSnapshotValue
    cachedSnapshotRaw = raw
    cachedSnapshotValue = readActiveUpdateSession()
    return cachedSnapshotValue
  } catch {
    return null
  }
}

export function useActiveUpdateSession(): ActiveUpdateSession | null {
  return useSyncExternalStore(subscribeActiveUpdateSession, getSessionSnapshot, () => null)
}

/**
 * 全局更新状态感知：
 * - 在线更新阶段（下载/等待/拉取镜像）：在顶部展示轻量的“正在更新中”进度条，不阻断浏览；
 * - 服务重启断连阶段（Hub 容器切换 / Worker Velopack 替换重启）：明确展示“正在更新重启中”与已用时，
 *   与“服务挂了”区分开，并持续每 2 秒自动重连，版本就绪后自动刷新一次。
 */
export function useGlobalUpdateOverlayState() {
  const role = useRole()
  const statusQuery = useUpdateStatus(true)
  const session = useActiveUpdateSession()
  const status = statusQuery.data

  // 将后端活跃更新状态同步到 localStorage，保证服务断开或刷新页面后仍能识别正在更新。
  useEffect(() => {
    if (!status) return
    if (status.flow.active) {
      writeActiveUpdateSession({
        targetVersion: status.flow.version || status.latest?.version || '',
        fromVersion: status.current_version,
        role: status.role || role.role,
        stage: status.flow.stage,
        message: status.flow.message,
        progress: status.flow.progress,
      })
      return
    }
    if (session) {
      clearActiveUpdateSession()
    }
  }, [status, role.role, session])

  const disconnected = statusQuery.isError || Boolean(role.error) || (statusQuery.isPending && !status)
  const isUpdatingActive = Boolean(status?.flow.active)
  const isRestartingDisconnected = Boolean(session && disconnected)
  const targetVersion = status?.flow.version || session?.targetVersion || ''
  const activeStage: UpdateFlowStage = status?.flow.active
    ? status.flow.stage
    : isRestartingDisconnected
      ? (session?.stage === 'downloading' ? 'restarting' : session?.stage || 'restarting')
      : 'idle'

  return {
    statusQuery,
    status,
    session,
    disconnected,
    isUpdatingActive,
    isRestartingDisconnected,
    targetVersion,
    activeStage,
  }
}

/** 顶部轻量进度条：服务在线且后台正在下载/安装更新时显示在所有页面顶部。 */
export function UpdateProgressBanner() {
  const { status, isUpdatingActive, isRestartingDisconnected, targetVersion, activeStage } = useGlobalUpdateOverlayState()

  if (!isUpdatingActive || isRestartingDisconnected || !status) return null

  const label = STAGE_LABEL[activeStage] || '正在更新中'
  const progress = status.flow.progress
  const showBar = activeStage === 'downloading' && progress > 0 && progress <= 100

  return (
    <div
      role="status"
      aria-live="polite"
      className="flex flex-wrap items-center justify-between gap-3 border-b border-subtle bg-secondary-soft px-4 py-2 text-xs text-default md:px-6"
    >
      <div className="flex min-w-0 flex-1 items-center gap-2.5">
        <StatusBadge tone="warning">正在更新</StatusBadge>
        <span className="truncate font-medium">
          {targetVersion ? `正在更新至 v${targetVersion}` : '正在更新'}
          {' · '}
          {label}
        </span>
        {status.flow.message ? (
          <span className="hidden truncate text-secondary lg:inline">{status.flow.message}</span>
        ) : null}
        {showBar ? (
          <span className="shrink-0 tabular-nums text-secondary">{progress}%</span>
        ) : null}
      </div>
      <Link
        to="/settings#updates-software"
        className="shrink-0 text-xs font-medium text-default underline underline-offset-2 hover:text-secondary"
      >
        查看进度
      </Link>
    </div>
  )
}

/** 全屏友好重连遮罩：仅在“已知正在更新”且后端因重启短暂断连时接管主区域，避免误报服务挂了。 */
export function UpdatingReconnectOverlay() {
  const queryClient = useQueryClient()
  const role = useRole()
  const { statusQuery, status, session, isRestartingDisconnected, targetVersion, activeStage } = useGlobalUpdateOverlayState()
  const [now, setNow] = useState(() => Date.now())
  const [dismissed, setDismissed] = useState(false)

  // 服务重启恢复并确认已切到新版本时，自动刷新页面一次加载新版前端资源。
  useEffect(() => {
    if (!status || status.flow.active) return
    if (status.flow.stage !== 'completed' || !status.flow.version) return
    if (status.current_version !== status.flow.version) return
    if (!session?.fromVersion || session.fromVersion === status.current_version) return
    try {
      const key = 'menu-capture-update-reloaded'
      if (window.sessionStorage.getItem(key) === status.flow.version) return
      window.sessionStorage.setItem(key, status.flow.version)
      window.setTimeout(() => window.location.reload(), 600)
    } catch {
      // 存储不可用时仅刷新查询缓存。
      void queryClient.invalidateQueries()
    }
  }, [status, session?.fromVersion, queryClient])

  useEffect(() => {
    if (!isRestartingDisconnected) {
      setDismissed(false)
      return
    }
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [isRestartingDisconnected])

  if (!isRestartingDisconnected || dismissed || !session) return null

  const elapsedMs = Math.max(0, now - session.startedAt)
  const stepIndex = stageToStepIndex(activeStage, true)
  const isHub = (session.role || role.role) === 'hub'
  const stageText = STAGE_LABEL[activeStage] || '正在重启服务'
  const roleDesc = isHub
    ? '正在切换新版共享后台容器并等待健康检查，期间接口会短暂断开，完成后页面会自动重连并刷新。'
    : '正在替换 Windows 采集端程序并执行新版本启动验证，期间服务会短暂断开，完成后页面会自动重连并刷新。'

  const handleManualRetry = () => {
    void statusQuery.refetch()
    role.reload()
  }

  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed inset-0 z-50 flex items-center justify-center bg-surface/95 px-4 py-8 backdrop-blur-sm"
    >
      <div className="w-full max-w-lg rounded-2xl border border-subtle bg-surface p-6 shadow-lg">
        <div className="flex items-center justify-between gap-3">
          <StatusBadge tone="warning">正在更新中</StatusBadge>
          <span className="text-xs tabular-nums text-tertiary">已用时 {formatElapsed(elapsedMs)}</span>
        </div>

        <h2 className="mt-4 heading-lg text-default">
          {targetVersion ? `正在更新至 v${targetVersion}，服务重启中…` : '正在更新并重启服务…'}
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-secondary">{roleDesc}</p>

        <div className="mt-5 rounded-xl border border-subtle bg-surface-secondary px-4 py-3">
          <div className="flex items-center justify-between gap-2 text-xs">
            <span className="font-medium text-default">{stageText}</span>
            <span className="text-tertiary">每 2 秒自动重连</span>
          </div>
          {session.message ? (
            <p className="mt-1 text-xs text-secondary">{session.message}</p>
          ) : null}
          <div className="mt-3 grid grid-cols-4 gap-2">
            {STEP_LIST.map((step, idx) => {
              const done = idx < stepIndex
              const current = idx === stepIndex
              return (
                <div key={step.key} className="space-y-1">
                  <div
                    className={[
                      'h-1.5 w-full rounded-full transition-colors',
                      done ? 'bg-primary-solid' : current ? 'animate-pulse bg-primary-solid/70' : 'bg-subtle',
                    ].join(' ')}
                  />
                  <p className={['text-2xs', current || done ? 'text-default' : 'text-tertiary'].join(' ')}>
                    {step.label}
                  </p>
                </div>
              )
            })}
          </div>
        </div>

        <p className="mt-4 text-xs text-tertiary">
          这是正常的更新重启过程，并非服务异常崩溃；你可以停留在当前页等待自动恢复，也可以关闭页面稍后再打开。
        </p>

        <div className="mt-5 flex flex-wrap items-center justify-end gap-2">
          {elapsedMs >= 60_000 ? (
            <Button color="secondary" variant="ghost" size="sm" onClick={() => setDismissed(true)}>
              暂时收起
            </Button>
          ) : null}
          <Button color="secondary" variant="outline" size="sm" onClick={handleManualRetry}>
            立即重试连接
          </Button>
        </div>
      </div>
    </div>
  )
}
