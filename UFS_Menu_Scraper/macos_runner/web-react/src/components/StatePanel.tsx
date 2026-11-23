import type { ReactNode } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { EmptyMessage } from '@openai/apps-sdk-ui/components/EmptyMessage'
import { LoadingIndicator } from '@openai/apps-sdk-ui/components/Indicator'

export type StateVariant = 'loading' | 'empty' | 'error' | 'info'

interface StatePanelProps {
  variant: StateVariant
  title: string
  description?: string
  action?: ReactNode
}

/** 加载、空数据、无匹配、失败和权限提示的统一容器，形态取自官方组件。 */
export function StatePanel({ variant, title, description, action }: StatePanelProps) {
  if (variant === 'loading') {
    return (
      <div
        className="flex flex-col items-center justify-center gap-3 px-6 py-12 text-center text-sm text-secondary"
        role="status"
        aria-live="polite"
      >
        <LoadingIndicator size={20} />
        <span className="text-sm text-default">{title}</span>
        {description ? <span className="max-w-lg leading-relaxed">{description}</span> : null}
      </div>
    )
  }

  if (variant === 'empty') {
    return (
      <EmptyMessage className="px-6 py-12">
        <EmptyMessage.Title>{title}</EmptyMessage.Title>
        {description ? <EmptyMessage.Description>{description}</EmptyMessage.Description> : null}
        {action ? <EmptyMessage.ActionRow>{action}</EmptyMessage.ActionRow> : null}
      </EmptyMessage>
    )
  }

  return (
    <Alert
      color={variant === 'error' ? 'danger' : 'info'}
      variant="soft"
      title={title}
      description={description}
      actions={action}
      actionsPlacement="end"
    />
  )
}
