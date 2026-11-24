import type { ReactNode } from 'react'
import { Badge } from '@openai/apps-sdk-ui/components/Badge'

export type StatusTone = 'success' | 'danger' | 'warning' | 'info' | 'secondary'

const BADGE_COLOR: Record<StatusTone, 'success' | 'danger' | 'warning' | 'info' | 'secondary' | 'discovery'> = {
  success: 'success',
  danger: 'danger',
  warning: 'warning',
  info: 'info',
  secondary: 'secondary',
}

interface StatusBadgeProps {
  tone: StatusTone
  children: ReactNode
}

/** 状态同时带文字与颜色，避免只靠颜色表达业务结果。 */
export function StatusBadge({ tone, children }: StatusBadgeProps) {
  return (
    <Badge color={BADGE_COLOR[tone]} variant="soft" size="sm">
      {children}
    </Badge>
  )
}
