import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '../../api/client'
import { useToast } from '../../components/ToastProvider'
import type { SharedStatus } from '../../api/types'
import { roleOf } from '../../api/types'
import { fetchActiveTasks, fetchTaskDetail } from '../task-center/api'
import type { ManualState } from '../worker/types'
import { isOnline } from '../hub/format'
import {
  activateNotificationAudio,
  claimNotificationEvent,
  deliverNotificationSound,
  getOrCreateWaitStart,
  manualWaitEventId,
  playNotificationSound,
  readNotificationPreferences,
  transitionNotificationState,
  type NotificationEvent,
  type NotificationEventKind,
} from './preferences'

interface ManualControlState {
  device_id: string
  device_name: string
  run_name: string
  run_status: string
  manual: ManualState | null
}

const TERMINAL_SUCCESS = new Set(['completed', 'complete', 'success', 'succeeded'])
const TERMINAL_ABNORMAL = new Set(['failed', 'terminated', 'cancelled', 'interrupted', 'blocked'])
const TERMINAL = new Set([...TERMINAL_SUCCESS, ...TERMINAL_ABNORMAL])

/**
 * 只从服务端状态转换发出提醒。通知偏好与去重记录留在当前操作者浏览器，
 * 不向 Hub/Worker 写入配置，也不把提醒当成业务动作。
 */
export function NotificationRuntime() {
  const toast = useToast()
  // 远程控制是受限路由，不允许访问共享状态和任务历史 API。
  const remoteControl = window.location.pathname === '/remote-control'
  const roleQuery = useQuery({
    queryKey: ['shared-status'],
    queryFn: ({ signal }) => apiFetch<SharedStatus>('/api/shared/status?include_history=false', { signal }),
    enabled: !remoteControl,
    staleTime: 15_000,
    refetchInterval: 30_000,
  })
  const role = remoteControl ? 'worker' : roleOf(roleQuery.data)
  const tasksQuery = useQuery({
    queryKey: ['tasks-active'],
    queryFn: ({ signal }) => fetchActiveTasks(signal),
    enabled: !remoteControl,
    refetchInterval: 5_000,
    staleTime: 4_000,
  })
  const manualQuery = useQuery({
    queryKey: ['manual-control'],
    queryFn: ({ signal }) => apiFetch<ManualControlState>('/api/manual/state', { signal }),
    enabled: role === 'worker',
    refetchInterval: 1_500,
    refetchIntervalInBackground: true,
    staleTime: 1_250,
  })

  const tasks = tasksQuery.data?.tasks
  const manual = manualQuery.data
  const sharedStatus = roleQuery.data
  const manualWaitKey = manual?.manual?.waiting && !manual.manual.decision && manual.manual.wait_id && manual.manual.run_id && manual.device_id
    ? manualWaitEventId(manual.device_id, manual.manual.run_id, manual.manual.wait_id)
    : ''
  const manualSnapshot = useRef(manual)
  manualSnapshot.current = manual
  const [preferenceRevision, setPreferenceRevision] = useState(0)
  const [isDocumentVisible, setDocumentVisible] = useState(() => document.visibilityState === 'visible')
  useEffect(() => {
    const refresh = () => setPreferenceRevision((value) => value + 1)
    const updateVisibility = () => setDocumentVisible(document.visibilityState === 'visible')
    const unlock = () => {
      const current = readNotificationPreferences()
      if (current.sound && current.volume > 0) void activateNotificationAudio()
    }
    window.addEventListener('pointerdown', unlock)
    window.addEventListener('keydown', unlock)
    window.addEventListener('storage', refresh)
    window.addEventListener('menu-capture-notification-preferences', refresh)
    document.addEventListener('visibilitychange', updateVisibility)
    return () => {
      window.removeEventListener('pointerdown', unlock)
      window.removeEventListener('keydown', unlock)
      window.removeEventListener('storage', refresh)
      window.removeEventListener('menu-capture-notification-preferences', refresh)
      document.removeEventListener('visibilitychange', updateVisibility)
    }
  }, [])
  const preferences = useMemo(() => readNotificationPreferences(), [preferenceRevision])
  const deviceName = useMemo(() => {
    if (sharedStatus?.role === 'worker') return sharedStatus.name || '采集电脑'
    return ''
  }, [sharedStatus])

  useEffect(() => {
    let disposed = false
    const backgroundDeliveryAvailable = preferences.desktop && canUseDesktopNotifications() && Notification.permission === 'granted'
    const emit = async (event: NotificationEvent) => {
      const currentPreferences = readNotificationPreferences()
      if (disposed || !currentPreferences.enabled[event.kind]) return
      if (currentPreferences.sound && currentPreferences.volume > 0) {
        void deliverNotificationSound(event.id, () => {
          if (disposed || (event.kind === 'manual_wait' && !manualQueryDataStillWaiting(manualSnapshot.current, event.id))) return Promise.resolve(false)
          const latest = readNotificationPreferences()
          return latest.sound && latest.enabled[event.kind] ? playNotificationSound(latest.volume, event.kind, false) : Promise.resolve(false)
        })
      }
      // 后台声音不依赖桌面通知权限。视觉提示仅在可显示时领取。
      if ((!isDocumentVisible && !backgroundDeliveryAvailable) || !(await claimNotificationEvent(event.id)) || disposed) return
      toast.push(event.body, event.kind === 'abnormal' || event.kind === 'sync_failure' ? 'danger' : 'info')
      if (currentPreferences.desktop && canUseDesktopNotifications() && Notification.permission === 'granted') {
        try {
          const notification = new Notification(event.title, { body: event.body, tag: event.id, silent: true })
          notification.onclick = () => {
            window.focus()
            if (event.route && window.location.pathname !== '/remote-control') window.location.hash = event.route
            notification.close()
          }
        } catch {
          // Desktop notifications can be blocked by the browser/OS; the in-app toast remains.
        }
      }
    }

    const manualState = manual?.manual
    if (manual && Date.now() - manualQuery.dataUpdatedAt <= 5_000 && manualState && manualState.waiting && !manualState.decision && typeof manualState.wait_id === 'string'
      && typeof manualState.run_id === 'string' && manual.device_id) {
      const state = manualState
      const runId = state.run_id as string
      const waitId = state.wait_id as string
      const id = manualWaitEventId(manual.device_id, runId, waitId)
      const store = state.store_name || state.task_id || '当前门店'
      void emit({
        id,
        kind: 'manual_wait',
        title: `需要人工操作 · ${manual.device_name || deviceName || '采集电脑'}`,
        body: `${store} 正在等待人工操作。请进入菜单后开始截图，或按实际情况标记不合格。`,
        route: '/workbench',
      })
    }

    if (tasks && Date.now() - tasksQuery.dataUpdatedAt <= 15_000) {
      for (const task of tasks) {
        const transition = transitionNotificationState(`task.${task.source}.${task.id}`, task.status)
        void transition.then(({ previous, changedAt }) => {
          if (disposed || !previous || previous === task.status || !TERMINAL.has(task.status)) return
          const isSuccess = TERMINAL_SUCCESS.has(task.status)
          const kind: NotificationEventKind = isSuccess
            ? task.source === 'materials' ? 'materials_complete' : 'task_complete'
            : 'abnormal'
          const failedCount = Number(task.extra.failed) || 0
          const fallbackSummary = `${task.progress.done} / ${task.progress.total} 家${task.source === 'materials' ? ` · 失败 ${failedCount}` : ''}`
          const route = `/task-center?${new URLSearchParams({ detail: `${task.source}:${task.id}` })}`
          const title = isSuccess ? `${task.source === 'materials' ? '素材分析' : '采集任务'}已完成` : `${task.source === 'materials' ? '素材分析' : '采集任务'}异常结束`
          const eventId = `task-terminal:${task.source}:${task.id}:${task.status}:${changedAt}`
          const send = (summary: string, detailMessage = '') => {
            const completion = isSuccess && task.source === 'capture' ? '；结果是否同步需单独确认。' : ''
            const failure = !isSuccess ? `。${detailMessage || '请打开任务中心查看运行详情及可恢复操作。'}` : ''
            void emit({
              id: eventId,
              kind,
              title,
              body: `${task.title || task.id} · ${summary}${completion}${failure}`,
              route,
            })
          }
          if (task.source === 'capture') {
            void fetchTaskDetail('capture', task.id).then((detail) => {
              const counts = detail.counts
              const summary = counts
                ? `已截图 ${counts.done ?? 0} · 待检查 ${counts.attention ?? 0} · 不合格 ${counts.rejected ?? 0} · 共 ${counts.total ?? task.progress.total} 家`
                : fallbackSummary
              send(summary, detail.message)
            }).catch(() => send(fallbackSummary))
          } else send(fallbackSummary)
        })
      }
    }

    if (sharedStatus?.role === 'worker') {
      const connection = sharedStatus.credential_invalid
        ? 'credential-invalid'
        : sharedStatus.paired && !sharedStatus.online
          ? 'offline'
          : sharedStatus.history_error
            ? `sync-error:${sharedStatus.history_error.slice(0, 100)}`
            : sharedStatus.paired && sharedStatus.online
              ? 'healthy'
              : 'unpaired'
      void transitionNotificationState(`worker-sync.${window.location.origin}`, connection).then(({ previous, changedAt }) => {
        if (disposed || !previous || previous === connection || connection === 'healthy' || connection === 'unpaired') return
        const reason = connection === 'offline'
          ? '共享后台连接中断。采集仍以本机状态为准，恢复连接后再核对补传结果。'
          : connection === 'credential-invalid'
            ? '共享后台配对凭证已失效，请联系管理员处理。'
            : connection.startsWith('sync-error:')
              ? `采集结果同步失败：${connection.slice('sync-error:'.length)}`
              : ''
        if (!reason) return
        void emit({
          id: `sync-failure:${window.location.origin}:${connection}:${changedAt}`,
          kind: 'sync_failure',
          title: '共享后台同步需要处理',
          body: reason,
          route: '/settings',
        })
      })
    }

    if (sharedStatus?.role === 'hub') {
      for (const worker of sharedStatus.workers) {
        if (worker.revoked_at || worker.archived_at) continue
        const state = isOnline(worker.last_seen) ? 'online' : 'offline'
        void transitionNotificationState(`hub-device.${worker.id}`, state).then(({ previous, changedAt }) => {
          if (disposed || !previous || previous === state) return
          void emit({
            id: `hub-device:${worker.id}:${state}:${changedAt}`,
            kind: 'device_offline',
            title: state === 'offline' ? '采集电脑已离线' : '采集电脑已恢复在线',
            body: `${worker.name || worker.id}${state === 'offline'
              ? ' 超过 60 秒未收到心跳。请查看设备连接状态和待补传数量。'
              : ' 已重新连接。请核对待补传及同步状态，心跳恢复不代表结果已全部同步。'}`,
            route: '/devices',
          })
        })
      }
    }

    return () => { disposed = true }
  }, [deviceName, isDocumentVisible, manual, manualQuery.dataUpdatedAt, preferences.desktop, preferenceRevision, sharedStatus, tasks, tasksQuery.dataUpdatedAt, toast])

  // 可选的重复等待只重复声音；wait_id 改变、结束或取消偏好后立即清理计时。
  useEffect(() => {
    const state = manual?.manual
    if (!manualWaitKey || !state?.waiting || state.decision || !preferences.sound || !preferences.enabled.manual_wait || !preferences.repeatWaitMinutes) return
    let timer = 0
    let cancelled = false
    const minutes = preferences.repeatWaitMinutes
    const waitId = manualWaitKey
    void getOrCreateWaitStart(waitId).then((startedAt) => {
      if (cancelled) return
      const interval = minutes * 60_000
      const schedule = () => {
        const elapsed = Date.now() - startedAt
        const nextIndex = Math.floor(elapsed / interval) + 1
        timer = window.setTimeout(async () => {
          if (cancelled) return
          const current = manualQueryDataStillWaiting(manualSnapshot.current, waitId)
          if (current) {
            const latest = readNotificationPreferences()
            if (latest.sound && latest.enabled.manual_wait) void deliverNotificationSound(`manual-repeat:${waitId}:${nextIndex}`, () => {
              return !cancelled && manualQueryDataStillWaiting(manualSnapshot.current, waitId) ? playNotificationSound(latest.volume, 'manual_wait', false) : Promise.resolve(false)
            })
          }
          if (current && !cancelled) schedule()
        }, Math.max(500, nextIndex * interval - elapsed))
      }
      schedule()
    })
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [manualWaitKey, preferences.enabled.manual_wait, preferences.repeatWaitMinutes, preferences.sound])

  return null
}

function manualQueryDataStillWaiting(value: ManualControlState | undefined, expectedId: string): boolean {
  const manual = value?.manual
  return Boolean(value?.device_id && manual?.waiting && !manual.decision && manual.wait_id && manual.run_id
    && manualWaitEventId(value.device_id, manual.run_id, manual.wait_id) === expectedId)
}

function canUseDesktopNotifications(): boolean {
  return typeof window !== 'undefined' && window.isSecureContext && 'Notification' in window
}
