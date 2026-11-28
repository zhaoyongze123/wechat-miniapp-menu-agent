import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Badge } from '@openai/apps-sdk-ui/components/Badge'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Bell, NotificationOffBell, Settings } from '@openai/apps-sdk-ui/components/Icon'
import { AppDrawer } from '../../components/AppDrawer'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge, type StatusTone } from '../../components/StatusBadge'
import { useNotificationCenter, type NotificationItem, type NotificationSeverity } from './store'

type FilterTab = 'all' | 'unread' | 'active_alerts'

const SEVERITY_META: Record<NotificationSeverity, { label: string; tone: StatusTone }> = {
  danger: { label: '需处理', tone: 'danger' },
  warning: { label: '注意', tone: 'warning' },
  success: { label: '已完成', tone: 'success' },
  info: { label: '提醒', tone: 'info' },
}

function formatRelativeTime(timestamp: number): string {
  const diff = Math.max(0, Date.now() - timestamp)
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  return new Date(timestamp).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function formatMuteUntil(until: number): string {
  return new Date(until).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

/** 顶栏通知中心入口与抽屉：集中承载异步事件、未读状态、自动自愈与免打扰控制。 */
export function NotificationCenterTrigger() {
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<FilterTab>('all')
  const navigate = useNavigate()
  const center = useNotificationCenter()

  const filtered = useMemo(() => {
    if (tab === 'unread') return center.items.filter((item) => item.readAt === null)
    if (tab === 'active_alerts') {
      return center.items.filter(
        (item) => item.resolvedAt === null && (item.severity === 'danger' || item.severity === 'warning'),
      )
    }
    return center.items
  }, [center.items, tab])

  const badgeCount = center.activeAlertCount > 0 ? center.activeAlertCount : center.unreadCount
  const badgeColor = center.activeAlertCount > 0 ? 'danger' : 'secondary'

  const openRoute = (item: NotificationItem) => {
    center.markRead(item.id)
    if (item.route) {
      setOpen(false)
      navigate(item.route)
    }
  }

  return (
    <>
      <Button
        type="button"
        color="secondary"
        variant="ghost"
        size="sm"
        aria-label={badgeCount > 0 ? `通知中心，${badgeCount} 条未处理提醒` : '打开通知中心'}
        onClick={() => setOpen(true)}
        className="relative gap-1.5"
      >
        {center.muted ? (
          <NotificationOffBell className="h-4 w-4 text-tertiary" aria-hidden="true" />
        ) : (
          <Bell className="h-4 w-4 text-secondary" aria-hidden="true" />
        )}
        {badgeCount > 0 ? (
          <Badge color={badgeColor} variant="soft" size="sm">
            {badgeCount > 99 ? '99+' : badgeCount}
          </Badge>
        ) : null}
      </Button>

      <AppDrawer
        open={open}
        onOpenChange={setOpen}
        title="通知中心"
        description={
          center.muted
            ? `免打扰已开启（至 ${formatMuteUntil(center.muteUntil)}），期间仅记录不发声。`
            : '集中展示采集任务、人工等待与设备连接事件；状态恢复后自动标记已恢复。'
        }
        closeLabel="关闭"
        headerActions={
          <div className="flex items-center gap-1">
            {center.unreadCount > 0 ? (
              <Button
                type="button"
                color="secondary"
                variant="ghost"
                size="sm"
                onClick={() => center.markAllRead()}
              >
                全部已读
              </Button>
            ) : null}
            <Button
              type="button"
              color="secondary"
              variant="ghost"
              size="sm"
              aria-label="通知偏好设置"
              onClick={() => {
                setOpen(false)
                navigate('/settings?section=notifications')
              }}
            >
              <Settings className="h-4 w-4" aria-hidden="true" />
            </Button>
          </div>
        }
        footer={
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-1.5">
              {center.muted ? (
                <Button
                  type="button"
                  color="secondary"
                  variant="soft"
                  size="sm"
                  onClick={() => center.setMuteMinutes(0)}
                >
                  取消免打扰
                </Button>
              ) : (
                <>
                  <Button
                    type="button"
                    color="secondary"
                    variant="ghost"
                    size="sm"
                    onClick={() => center.setMuteMinutes(30)}
                  >
                    静音 30 分钟
                  </Button>
                  <Button
                    type="button"
                    color="secondary"
                    variant="ghost"
                    size="sm"
                    onClick={() => center.setMuteMinutes(60)}
                  >
                    静音 1 小时
                  </Button>
                </>
              )}
            </div>
            {center.items.some((item) => item.resolvedAt !== null || item.readAt !== null) ? (
              <Button
                type="button"
                color="secondary"
                variant="ghost"
                size="sm"
                onClick={() => center.clearResolved()}
              >
                清理已读与已恢复
              </Button>
            ) : null}
          </div>
        }
      >
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-1.5 border-b border-subtle pb-3">
            <Button
              type="button"
              size="sm"
              color={tab === 'all' ? 'primary' : 'secondary'}
              variant={tab === 'all' ? 'soft' : 'ghost'}
              onClick={() => setTab('all')}
            >
              全部 ({center.items.length})
            </Button>
            <Button
              type="button"
              size="sm"
              color={tab === 'unread' ? 'primary' : 'secondary'}
              variant={tab === 'unread' ? 'soft' : 'ghost'}
              onClick={() => setTab('unread')}
            >
              未读 ({center.unreadCount})
            </Button>
            <Button
              type="button"
              size="sm"
              color={tab === 'active_alerts' ? 'primary' : 'secondary'}
              variant={tab === 'active_alerts' ? 'soft' : 'ghost'}
              onClick={() => setTab('active_alerts')}
            >
              待处理异常 ({center.activeAlertCount})
            </Button>
          </div>

          {filtered.length === 0 ? (
            <StatePanel
              variant="empty"
              title={tab === 'active_alerts' ? '暂无待处理异常' : tab === 'unread' ? '暂无未读通知' : '暂无通知记录'}
              description="任务完成、人工等待及持续连接异常会在这里统一记录，恢复后自动归档。"
            />
          ) : (
            <ul className="space-y-2.5" aria-label="通知列表">
              {filtered.map((item) => {
                const resolved = item.resolvedAt !== null
                const unread = item.readAt === null
                const badge = resolved
                  ? { label: '已恢复', tone: 'secondary' as StatusTone }
                  : SEVERITY_META[item.severity]
                return (
                  <li
                    key={item.id}
                    className={[
                      'rounded-lg border p-3.5 transition-colors',
                      unread ? 'border-default bg-surface-secondary' : 'border-subtle bg-surface',
                      resolved ? 'opacity-75' : '',
                    ].join(' ')}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0 flex-1 space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <StatusBadge tone={badge.tone}>{badge.label}</StatusBadge>
                          <p className="truncate text-sm font-medium text-default">{item.title}</p>
                          {item.count > 1 ? (
                            <Badge color="secondary" variant="soft" size="sm">
                              ×{item.count}
                            </Badge>
                          ) : null}
                        </div>
                        <p className="text-xs leading-relaxed text-secondary">{item.body}</p>
                        <div className="flex flex-wrap items-center gap-3 pt-1 text-2xs text-tertiary">
                          <span>{formatRelativeTime(item.updatedAt)}</span>
                          {item.route ? (
                            <Button
                              type="button"
                              color="primary"
                              variant="ghost"
                              size="sm"
                              onClick={() => openRoute(item)}
                            >
                              {item.actionLabel || '前往查看'}
                            </Button>
                          ) : null}
                          {unread ? (
                            <Button
                              type="button"
                              color="secondary"
                              variant="ghost"
                              size="sm"
                              onClick={() => center.markRead(item.id)}
                            >
                              标为已读
                            </Button>
                          ) : null}
                        </div>
                      </div>
                      <Button
                        type="button"
                        color="secondary"
                        variant="ghost"
                        size="sm"
                        aria-label="删除此条通知"
                        onClick={() => center.remove(item.id)}
                      >
                        ×
                      </Button>
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </AppDrawer>
    </>
  )
}
