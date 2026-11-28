import { useEffect, useState } from 'react'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { Slider } from '@openai/apps-sdk-ui/components/Slider'
import { StatusBadge } from '../../components/StatusBadge'
import { SettingRow } from '../../components/SettingsLayout'
import type { AppRole } from '../../api/types'
import { EmailNotificationSettings } from './EmailNotificationSettings'
import {
  DEFAULT_NOTIFICATION_PREFERENCES,
  playNotificationSound,
  readNotificationPreferences,
  saveNotificationPreferences,
  type NotificationEventKind,
  type NotificationPreferences,
} from './preferences'

interface EventOption {
  key: NotificationEventKind
  label: string
  description: string
  roles: AppRole[]
}

/**
 * 事件文案使用业务语言，并标注真实来源与去重口径。
 * roles 与 NotificationRuntime 的实际角色分支保持一致，避免展示不会触发的开关。
 */
const EVENT_OPTIONS: EventOption[] = [
  {
    key: 'manual_wait',
    label: '等待人工操作',
    description: '半自动采集停在同一家门店等待人工操作时提醒一次；同一等待不会因刷新重复提醒。',
    roles: ['worker'],
  },
  {
    key: 'task_complete',
    label: '采集任务完成',
    description: '仅在本机观察到运行中的采集任务结束时提醒；提醒不代表结果已经同步到共享后台。',
    roles: ['hub', 'worker', 'standalone'],
  },
  {
    key: 'materials_complete',
    label: '素材分析完成',
    description: '素材分析任务结束时提醒，包含成功与失败结果。',
    roles: ['hub', 'worker', 'standalone'],
  },
  {
    key: 'abnormal',
    label: '异常中断或需恢复',
    description: '采集或分析异常结束、被终止或需要人工恢复时提醒，便于尽快处理。',
    roles: ['hub', 'worker', 'standalone'],
  },
  {
    key: 'sync_failure',
    label: '同步失败',
    description: '采集端与共享后台的连接中断、凭证失效或结果同步失败时提醒。',
    roles: ['worker'],
  },
  {
    key: 'device_offline',
    label: '采集电脑离线提醒',
    description: '共享后台按服务端心跳窗口与连续确认判断离线，自动过滤短时波动；设备恢复后自动归档。',
    roles: ['hub'],
  },
]

interface NotificationPreferencesPanelProps {
  /** 只展示当前角色真实会触发的事件与设置。 */
  role: AppRole
}

export function NotificationPreferencesPanel({ role }: NotificationPreferencesPanelProps) {
  const [preferences, setPreferences] = useState<NotificationPreferences>(() => readNotificationPreferences())
  const [permission, setPermission] = useState<NotificationPermission | 'unsupported' | 'insecure'>(() => permissionState())
  const [message, setMessage] = useState('')
  const canUseSound = typeof window !== 'undefined' && 'AudioContext' in window
  const events = EVENT_OPTIONS.filter((event) => event.roles.includes(role))
  const supportsRepeat = events.some((event) => event.key === 'manual_wait')

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

  const update = (next: NotificationPreferences, feedback?: string) => {
    setPreferences(next)
    saveNotificationPreferences(next)
    if (feedback) setMessage(feedback)
  }

  const requestDesktopPermission = async () => {
    if (!canUseDesktopNotifications()) return
    try {
      const result = await Notification.requestPermission()
      setPermission(result)
      if (result === 'granted') {
        update({ ...preferences, desktop: true }, '桌面通知已启用。')
      } else {
        update({ ...preferences, desktop: false }, result === 'denied' ? '浏览器已拒绝通知；可在站点设置中更改。' : '尚未授予通知权限。')
      }
    } catch {
      setMessage('浏览器未能请求通知权限；站内提醒仍可使用。')
    }
  }

  const toggleDesktop = (checked: boolean) => {
    if (!checked) {
      update({ ...preferences, desktop: false }, '已关闭桌面通知。')
      return
    }
    if (permission === 'granted') {
      update({ ...preferences, desktop: true }, '已启用桌面通知。')
      return
    }
    void requestDesktopPermission()
  }

  const volumePercent = Math.round(preferences.volume * 100)
  const silent = volumePercent === 0

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm text-default">提醒事件</h3>
        <p className="mt-1 text-xs text-secondary">设置只保存在当前浏览器，不改变 Windows 执行配置或共享后台配置。</p>
      </div>

      <div className="space-y-3">
        {events.map((event) => (
          <div key={event.key} className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <Checkbox
                label={event.label}
                checked={preferences.enabled[event.key]}
                onCheckedChange={(checked) =>
                  update(
                    { ...preferences, enabled: { ...preferences.enabled, [event.key]: checked } },
                    checked ? `已开启“${event.label}”提醒。` : `已关闭“${event.label}”提醒。`,
                  )
                }
              />
              <p className="mt-1 pl-6 text-2xs leading-relaxed text-tertiary">{event.description}</p>
            </div>
          </div>
        ))}
        {events.length === 0 ? <p className="text-xs text-secondary">当前角色没有可配置的提醒事件。</p> : null}
      </div>

      <div className="grid gap-4 border-t border-subtle pt-4 sm:grid-cols-2">
        <div className="space-y-3">
          <SettingRow label="提示音" description="浏览器可能阻止后台自动播放；首次使用请先点击“测试提示音”。">
            <Checkbox
              label={preferences.sound ? '启用提示音' : '已静音'}
              checked={preferences.sound}
              disabled={!canUseSound}
              onCheckedChange={(checked) => update({ ...preferences, sound: checked }, checked ? '已启用提示音。' : '已关闭提示音。')}
            />
          </SettingRow>
          <div className="space-y-1.5">
            <Slider
              label="提示音音量"
              value={volumePercent}
              min={0}
              max={100}
              step={5}
              unit="%"
              disabled={!canUseSound || !preferences.sound}
              onChange={(value) => update({ ...preferences, volume: value / 100 })}
            />
            {silent ? <p className="text-xs text-warning">当前音量为 0，等同于静音。</p> : null}
            <p className="text-2xs text-tertiary">可用方向键微调；0% 等同于静音。</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              disabled={!canUseSound || !preferences.sound || silent}
              onClick={() => {
                void playNotificationSound(preferences.volume, 'task_complete').then((played) => {
                  setMessage(played ? '已播放测试提示音。' : '浏览器阻止了音频播放；请检查站点声音设置后重试。')
                })
              }}
            >
              测试提示音
            </Button>
            {!canUseSound ? <span className="text-2xs text-tertiary">当前浏览器不支持 Web Audio。</span> : null}
            {canUseSound && silent ? <span className="text-2xs text-tertiary">音量为 0，测试前请先调高音量。</span> : null}
          </div>
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
          <p className="text-2xs text-tertiary">需安全上下文及浏览器支持。拒绝权限不会影响站内提醒与提示音。</p>
        </div>
      </div>

      {supportsRepeat ? (
        <SettingRow
          label="等待人工操作的重复提醒"
          description="只重复提示音，不重复弹窗；等待结束或关闭提示音后自动停止。"
          htmlFor="repeat-wait"
        >
          <Select
            id="repeat-wait"
            aria-label="等待人工操作的重复提醒"
            value={String(preferences.repeatWaitMinutes)}
            options={[
              { value: '0', label: '关闭（默认）' },
              { value: '1', label: '每 1 分钟重复提示音' },
              { value: '5', label: '每 5 分钟重复提示音' },
            ]}
            onChange={(option) =>
              update(
                { ...preferences, repeatWaitMinutes: Number(option.value) as NotificationPreferences['repeatWaitMinutes'] },
                '重复提醒设置已保存。',
              )
            }
          />
        </SettingRow>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-subtle pt-4">
        <p className="text-2xs text-tertiary" role="status" aria-live="polite">
          {message || '关闭所有页面后不会继续接收提醒；浏览器后台节流可能延迟提醒。'}
        </p>
        <Button
          color="secondary"
          variant="ghost"
          size="sm"
          onClick={() => {
            update({ ...DEFAULT_NOTIFICATION_PREFERENCES, enabled: { ...DEFAULT_NOTIFICATION_PREFERENCES.enabled } }, '已恢复此浏览器的通知默认值。')
          }}
        >
          恢复默认
        </Button>
      </div>

      <EmailNotificationSettings />
    </div>
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
