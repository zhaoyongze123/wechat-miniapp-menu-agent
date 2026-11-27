import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { errorMessage } from '../../api/client'
import { statusOf } from '../task-center/format'
import { ManualPanel } from '../worker/ManualPanel'
import { activateNotificationAudio, playNotificationSound, readNotificationPreferences } from '../notifications/preferences'
import {
  fetchManualState,
  manualCapture,
  manualCommandContext,
  manualRecognizeStoreName,
  manualReject,
  manualSaveStoreName,
} from '../worker/api'

interface DocumentPictureInPicture {
  window: Window | null
  requestWindow: (options: { width: number; height: number }) => Promise<Window>
}

type Decision = { kind: 'capture' } | { kind: 'reject'; reason: string; detail: string }

export function RemoteControlPage() {
  // /remote-control 是独立轻量控制入口（未挂载 ProjectProvider，远程 HTTPS 亦禁止调用 /api/projects）；
  // 优先读取 URL 可选 project_id，否则由后端 /api/manual/state 自动绑定当前运行轮次所属项目。
  const urlProjectId = new URLSearchParams(window.location.search).get('project_id') || ''
  const [pinned, setPinned] = useState<Window | null>(null)
  const opening = useRef(false)
  const [windowError, setWindowError] = useState('')
  const [soundMessage, setSoundMessage] = useState('')

  async function testSound() {
    const prefs = readNotificationPreferences()
    if (!prefs.sound || prefs.volume <= 0) {
      setSoundMessage('当前浏览器提示音已关闭或音量为零。')
      return
    }
    const played = await playNotificationSound(prefs.volume, 'manual_wait')
    setSoundMessage(played ? '已发起测试提示音。' : '音频未激活，请在原始控制页点击测试提示音。')
  }

  const api = (window as Window & { documentPictureInPicture?: DocumentPictureInPicture }).documentPictureInPicture
  const query = useQuery({
    queryKey: ['remote-manual-control', urlProjectId || 'active'],
    queryFn: ({ signal }) => fetchManualState(signal, urlProjectId || undefined),
    refetchInterval: 1_500,
    refetchIntervalInBackground: Boolean(pinned),
    staleTime: 1_250,
  })
  const state = query.data
  const manual = state?.manual && state.manual.task_id ? state.manual : null
  const effectiveProjectId = urlProjectId || state?.project_id || undefined

  const requireFreshContext = () => {
    const context = manualCommandContext(state)
    if (!context || query.isError || Date.now() - query.dataUpdatedAt > 5_000) {
      throw new Error('当前状态已过期，请刷新后再操作')
    }
    return context
  }

  const command = useMutation({
    mutationFn: (decision: Decision) => {
      const context = requireFreshContext()
      return decision.kind === 'capture'
        ? manualCapture(context, effectiveProjectId)
        : manualReject(context, decision.reason, decision.detail, effectiveProjectId)
    },
    onSettled: () => {
      void query.refetch()
    },
  })

  const lastWait = useRef(manual?.wait_id)
  useEffect(() => {
    if (lastWait.current === manual?.wait_id) return
    lastWait.current = manual?.wait_id
    command.reset()
  }, [command.reset, manual?.wait_id])

  async function openPinned() {
    if (!api || opening.current) return
    if (api.window && !api.window.closed) {
      api.window.focus()
      return
    }
    opening.current = true
    if (readNotificationPreferences().sound) void activateNotificationAudio()
    setWindowError('')
    try {
      const child = await api.requestWindow({ width: 388, height: 468 })
      child.document.title = '半自动采集辅助'
      child.document.documentElement.lang = document.documentElement.lang || 'zh-CN'
      child.document.documentElement.className = document.documentElement.className
      document.querySelectorAll('link[rel=stylesheet], style').forEach((node) => {
        child.document.head.append(child.document.importNode(node, true))
      })
      child.addEventListener('pagehide', () => setPinned(null), { once: true })
      setPinned(child)
    } catch {
      setWindowError('浏览器未能打开置顶窗，可以继续使用这个控制页。')
    } finally {
      opening.current = false
    }
  }

  const connected = !query.isError && Boolean(state)

  const extraSettings = (
    <div className="space-y-2 border-t border-subtle pt-2">
      <div className="flex flex-wrap items-center justify-between gap-2 text-2xs text-secondary">
        <span>{state?.device_name || '采集电脑'}</span>
        <span>
          {query.dataUpdatedAt ? `更新于 ${new Date(query.dataUpdatedAt).toLocaleTimeString()}` : '未同步'}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button color="secondary" variant="outline" size="sm" onClick={() => void testSound()}>
          测试提示音
        </Button>
        {pinned ? (
          <Button
            color="secondary"
            variant="ghost"
            size="sm"
            onClick={() => {
              pinned.close()
              window.focus()
            }}
          >
            返回控制页
          </Button>
        ) : null}
      </div>
      {soundMessage ? (
        <p className="text-2xs text-secondary" role="status">
          {soundMessage}
        </p>
      ) : null}
    </div>
  )

  const panel = (
    <main className="mx-auto max-w-md space-y-2.5 p-3">
      <header className="flex items-center justify-between gap-2 px-0.5">
        <h1 className="heading-xs truncate text-default" title={state?.run_name || '半自动采集辅助'}>
          {state?.run_name || '半自动采集辅助'}
        </h1>
        <span className="shrink-0 text-2xs text-secondary">
          {state?.run_status ? statusOf(state.run_status).label : '空闲'}
        </span>
      </header>

      {query.isError ? (
        <Alert color="warning" variant="soft" title="状态读取失败，操作已暂停" description={errorMessage(query.error)} />
      ) : null}

      {manual ? (
        <ManualPanel
          key={manual.task_id}
          manual={manual}
          compact
          busy={!connected || command.isPending || Boolean(manual.decision) || !manual.wait_id}
          error={command.error ? errorMessage(command.error) : ''}
          onCapture={() => command.mutate({ kind: 'capture' })}
          onReject={(reason, detail) => command.mutate({ kind: 'reject', reason, detail })}
          onRecognizeStoreName={async () => {
            const context = requireFreshContext()
            const res = await manualRecognizeStoreName(context, effectiveProjectId)
            void query.refetch()
            return res
          }}
          onSaveStoreName={async (storeName) => {
            const context = requireFreshContext()
            await manualSaveStoreName(context, storeName, effectiveProjectId)
            void query.refetch()
          }}
          extraSettings={extraSettings}
        />
      ) : query.isPending ? (
        <p className="rounded-xl border border-subtle bg-surface p-4 text-center text-xs text-secondary" role="status">
          正在读取采集状态…
        </p>
      ) : (
        <div className="rounded-xl border border-subtle bg-surface p-4 text-center text-xs text-secondary">
          当前没有运行中的半自动采集任务
        </div>
      )}
    </main>
  )

  return (
    <>
      {pinned ? createPortal(panel, pinned.document.body) : panel}
      <div className="mx-auto max-w-md space-y-2 px-3 pb-4">
        <div className="flex flex-wrap items-center gap-2">
          {window.isSecureContext && api ? (
            <Button color="secondary" variant="outline" size="sm" onClick={() => void openPinned()}>
              {pinned ? '查看置顶控制窗' : '打开置顶控制窗'}
            </Button>
          ) : (
            <p className="text-xs text-secondary">当前浏览器不支持置顶，可直接作为独立控制窗使用。</p>
          )}
          {window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1' ? (
            <a className="text-xs text-secondary underline hover:text-default" href="/#/workbench">
              返回采集工作台
            </a>
          ) : null}
        </div>
        {windowError ? (
          <p className="text-xs text-danger" role="status">
            {windowError}
          </p>
        ) : null}
      </div>
    </>
  )
}
