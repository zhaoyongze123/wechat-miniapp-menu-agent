import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { errorMessage } from '../../api/client'
import { statusOf } from '../task-center/format'
import { ManualPanel } from '../worker/ManualPanel'
import { activateNotificationAudio, playNotificationSound, readNotificationPreferences } from '../notifications/preferences'
import { fetchManualState, manualCapture, manualCommandContext, manualReject } from '../worker/api'
import { useProject } from '../projects/ProjectProvider'

interface DocumentPictureInPicture {
  window: Window | null
  requestWindow: (options: { width: number; height: number }) => Promise<Window>
}

type Decision = { kind: 'capture' } | { kind: 'reject'; reason: string; detail: string }

export function RemoteControlPage() {
  const { projectId } = useProject()
  const [pinned, setPinned] = useState<Window | null>(null)
  const opening = useRef(false)
  const [windowError, setWindowError] = useState('')
  const [soundMessage, setSoundMessage] = useState('')
  async function testSound() {
    const prefs = readNotificationPreferences()
    if (!prefs.sound || prefs.volume <= 0) { setSoundMessage('当前浏览器提示音已关闭或音量为零。'); return }
    const played = await playNotificationSound(prefs.volume, 'manual_wait')
    setSoundMessage(played ? '已发起测试提示音。' : '音频未激活，请在原始控制页点击测试提示音。')
  }
  const api = (window as Window & { documentPictureInPicture?: DocumentPictureInPicture }).documentPictureInPicture
  const query = useQuery({
    queryKey: ['project', projectId || 'unselected', 'manual-control'],
    queryFn: ({ signal }) => fetchManualState(signal, projectId),
    enabled: Boolean(projectId),
    refetchInterval: 1_500,
    refetchIntervalInBackground: Boolean(pinned),
    staleTime: 1_250,
  })
  const state = query.data
  const manual = state?.manual
  const command = useMutation({
    mutationFn: (decision: Decision) => {
      const context = manualCommandContext(state)
      if (!context || query.isError || Date.now() - query.dataUpdatedAt > 5_000) {
        throw new Error('当前状态已过期，请刷新后再操作')
      }
      return decision.kind === 'capture' ? manualCapture(context, projectId) : manualReject(context, decision.reason, decision.detail, projectId)
    },
    onSettled: () => { void query.refetch() },
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
    // 在打开画中画的同一次用户手势内尝试激活父页面音频，不等待后再调用 requestWindow。
    if (readNotificationPreferences().sound) void activateNotificationAudio()
    setWindowError('')
    try {
      const child = await api.requestWindow({ width: 352, height: 410 })
      child.document.title = '半自动控制'
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
  const panel = (
    <main className="mx-auto max-w-md space-y-3 p-3">
      <header className="min-w-0 space-y-1">
        <p className="text-xs text-secondary">{state?.device_name || '采集电脑'}</p>
        <h1 className="heading-sm break-words">{state?.run_name || '半自动控制'}</h1>
        <p className="text-xs text-secondary">
          {state?.run_status ? statusOf(state.run_status).label : '当前没有运行中的半自动任务'}
          {query.dataUpdatedAt ? ` · 更新于 ${new Date(query.dataUpdatedAt).toLocaleTimeString()}` : ''}
        </p>
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
        />
      ) : query.isPending ? <p role="status">正在读取采集状态…</p> : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button color="secondary" variant="ghost" size="sm" onClick={() => void testSound()}>测试提示音</Button>
        {soundMessage ? <span className="text-2xs text-secondary" role="status">{soundMessage}</span> : null}
      </div>
      {pinned ? (
        <Button color="secondary" variant="ghost" size="sm" onClick={() => { pinned.close(); window.focus() }}>
          返回控制页
        </Button>
      ) : null}
    </main>
  )

  return (
    <>
      {pinned ? createPortal(panel, pinned.document.body) : panel}
      <div className="mx-auto max-w-md space-y-2 px-3 pb-3">
        {window.isSecureContext && api ? (
          <Button color="secondary" variant="outline" size="sm" onClick={() => void openPinned()}>
            {pinned ? '查看置顶控制窗' : '打开置顶控制窗'}
          </Button>
        ) : <p className="text-xs text-secondary">当前浏览器或连接不支持置顶。此页可作为普通独立控制窗使用。</p>}
        {pinned ? <Button color="secondary" variant="ghost" size="sm" onClick={() => void testSound()}>测试提示音</Button> : null}
        {pinned && soundMessage ? <p className="text-xs text-secondary" role="status">{soundMessage}</p> : null}
        {windowError ? <p role="status" className="text-xs text-secondary">{windowError}</p> : null}
        <p className="text-xs text-secondary">关闭控制窗后，Windows 上的采集任务会继续运行。</p>
        {window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1' ? (
          <a className="text-sm underline" href="/#/workbench">返回采集工作台</a>
        ) : null}
      </div>
    </>
  )
}
