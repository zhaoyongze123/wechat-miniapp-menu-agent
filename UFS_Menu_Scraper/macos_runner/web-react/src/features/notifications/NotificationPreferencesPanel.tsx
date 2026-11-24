import { useEffect, useState } from 'react'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { StatusBadge } from '../../components/StatusBadge'
import {
  DEFAULT_NOTIFICATION_PREFERENCES,
  playNotificationSound,
  readNotificationPreferences,
  saveNotificationPreferences,
  type NotificationEventKind,
  type NotificationPreferences,
} from './preferences'

const EVENT_OPTIONS: { key: NotificationEventKind; label: string; description: string }[] = [
  { key: 'manual_wait', label: '半自动等待人工操作', description: '每个新的 wait_id 提醒一次；相同事件刷新或开新标签不会重复提醒。' },
  { key: 'task_complete', label: '采集任务完成', description: '仅在本机观察到运行态转为终态时提醒；提醒不代表结果已同步。' },
  { key: 'materials_complete', label: '素材分析完成', description: '观察到分析任务从运行态转为完成或失败时提醒。' },
  { key: 'abnormal', label: '异常中断或需恢复', description: '采集或分析进入失败、中断、待恢复等异常终态时提醒。' },
  { key: 'sync_failure', label: '共享同步失败', description: '采集端配对连接或历史同步从正常转为失败时提醒。' },
  { key: 'device_offline', label: '采集电脑离线/恢复', description: '共享端沿用 60 秒心跳口径，只在在线状态发生变化后提醒。' },
]

export function NotificationPreferencesPanel() {
  const [preferences, setPreferences] = useState<NotificationPreferences>(() => readNotificationPreferences())
  const [permission, setPermission] = useState<NotificationPermission | 'unsupported' | 'insecure'>(() => permissionState())
  const [message, setMessage] = useState('')
  const canUseSound = typeof window !== 'undefined' && 'AudioContext' in window

  useEffect(() => {
    const refresh = () => setPreferences(readNotificationPreferences())
    const syncPermission = () => setPermission(permissionState())
    window.addEventListener('storage', refresh)
    window.addEventListener('menu-capture-notification-preferences', refresh)
    document.addEventListener('visibilitychange', syncPermission)
    return () => {
      window.removeEventListener('storage', refresh)
      window.removeEventListener('menu-capture-notification-preferences', refresh)
      document.removeEventListener('visibilitychange', syncPermission)
    }
  }, [])

  const update = (next: NotificationPreferences) => {
    setPreferences(next)
    saveNotificationPreferences(next)
    setMessage('偏好已保存在此浏览器。')
  }

  const requestDesktopPermission = async () => {
    if (!canUseDesktopNotifications()) return
    try {
      const result = await Notification.requestPermission()
      setPermission(result)
      if (result === 'granted') {
        update({ ...preferences, desktop: true })
        setMessage('桌面通知已启用。')
      } else {
        update({ ...preferences, desktop: false })
        setMessage(result === 'denied' ? '浏览器已拒绝通知；可在站点设置中更改。' : '尚未授予通知权限。')
      }
    } catch {
      setMessage('浏览器未能请求通知权限；站内提醒仍可使用。')
    }
  }

  const toggleDesktop = (checked: boolean) => {
    if (!checked) {
      update({ ...preferences, desktop: false })
      return
    }
    if (permission === 'granted') {
      update({ ...preferences, desktop: true })
      return
    }
    void requestDesktopPermission()
  }

  return (
    <section className="space-y-4 rounded-xl border border-subtle bg-surface p-4" aria-labelledby="notification-settings-heading">
      <div>
        <h2 id="notification-settings-heading" className="heading-xs">通知与提醒</h2>
        <p className="mt-1 text-xs text-secondary">设置只保存在当前浏览器，不改变 Windows 执行配置或 Hub 共享配置。</p>
      </div>

      <div className="space-y-3">
        {EVENT_OPTIONS.map((event) => (
          <div key={event.key} className="flex items-start gap-3">
            <Checkbox
              label={event.label}
              checked={preferences.enabled[event.key]}
              onCheckedChange={(checked) => update({
                ...preferences,
                enabled: { ...preferences.enabled, [event.key]: checked },
              })}
            />
            <p className="pt-0.5 text-2xs text-tertiary">{event.description}</p>
          </div>
        ))}
      </div>

      <div className="grid gap-4 border-t border-subtle pt-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Checkbox
            label="启用提示音"
            checked={preferences.sound}
            disabled={!canUseSound}
            onCheckedChange={(checked) => update({ ...preferences, sound: checked })}
          />
          <label className="block text-xs text-secondary" htmlFor="notification-volume">提示音音量</label>
          <input
            id="notification-volume"
            className="w-full accent-current"
            type="range"
            min="0"
            max="1"
            step="0.05"
            value={preferences.volume}
            disabled={!canUseSound || !preferences.sound}
            onChange={(event) => update({ ...preferences, volume: Number(event.target.value) })}
          />
          <div className="flex flex-wrap items-center gap-2">
            <Button color="secondary" variant="outline" size="sm" disabled={!canUseSound} onClick={() => {
              void playNotificationSound(preferences.volume, 'task_complete').then((played) => {
                setMessage(played ? '已播放测试提示音。' : '浏览器阻止了音频播放；请检查站点声音设置后重试。')
              })
            }}>
              测试提示音
            </Button>
            {!canUseSound ? <span className="text-2xs text-tertiary">当前浏览器不支持 Web Audio。</span> : null}
          </div>
          <p className="text-2xs text-tertiary">浏览器可能阻止后台自动播放；可先点击测试提示音激活音频。提醒不会持续报警。</p>
        </div>

        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm text-default">桌面通知</span>
            <StatusBadge tone={permission === 'granted' && preferences.desktop ? 'success' : permission === 'denied' ? 'danger' : 'secondary'}>
              {permission === 'unsupported' ? '浏览器不支持' : permission === 'insecure' ? '需要安全连接' : permission === 'granted' ? preferences.desktop ? '已启用' : '权限已授予' : permission === 'denied' ? '权限已拒绝' : '尚未授权'}
            </StatusBadge>
          </div>
          {permission === 'granted' ? (
            <Checkbox label="允许桌面通知" checked={preferences.desktop} onCheckedChange={toggleDesktop} />
          ) : permission === 'denied' ? (
            <p className="text-xs text-secondary">请在浏览器的站点设置中允许通知，再回到此页启用。</p>
          ) : (
            <Button color="secondary" variant="outline" size="sm" disabled={permission === 'unsupported' || permission === 'insecure'} onClick={() => void requestDesktopPermission()}>
              请求桌面通知权限
            </Button>
          )}
          <p className="text-2xs text-tertiary">需安全上下文及浏览器支持。拒绝权限不会影响站内提醒。</p>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3 border-t border-subtle pt-4">
        <label className="text-sm text-default" htmlFor="repeat-wait">人工等待重复提醒</label>
        <select
          id="repeat-wait"
          className="rounded-md border border-subtle bg-surface px-2.5 py-1.5 text-sm text-default"
          value={preferences.repeatWaitMinutes}
          onChange={(event) => update({ ...preferences, repeatWaitMinutes: Number(event.target.value) as NotificationPreferences['repeatWaitMinutes'] })}
        >
          <option value={0}>关闭（默认）</option>
          <option value={1}>每 1 分钟重复提示音</option>
          <option value={5}>每 5 分钟重复提示音</option>
        </select>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-2xs text-tertiary" role="status" aria-live="polite">{message || '关闭所有页面后不会继续接收提醒。'}</p>
        <Button color="secondary" variant="ghost" size="sm" onClick={() => {
          update({ ...DEFAULT_NOTIFICATION_PREFERENCES, enabled: { ...DEFAULT_NOTIFICATION_PREFERENCES.enabled } })
          setMessage('已恢复此浏览器的通知默认值。')
        }}>
          恢复默认
        </Button>
      </div>
    </section>
  )
}

function permissionState(): NotificationPermission | 'unsupported' | 'insecure' {
  if (typeof window === 'undefined' || !('Notification' in window)) return 'unsupported'
  if (!window.isSecureContext) return 'insecure'
  return Notification.permission
}

function canUseDesktopNotifications(): boolean {
  return permissionState() !== 'unsupported' && permissionState() !== 'insecure'
}
