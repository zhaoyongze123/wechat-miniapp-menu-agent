import type { ReactNode } from 'react'

interface PageHeaderProps {
  /** 页面职责，使用简短名词或动宾短语。 */
  title: string
  headingId?: string
  /** 可选；只有提供操作前提、数据范围、状态或风险等新增上下文时才传入。 */
  description?: string
  /** 页面主要操作。 */
  actions?: ReactNode
}

/**
 * 统一页面头部：标题必填，描述可选且默认不传。
 * 语义层级固定为 h1；模块标题由页面内 h2 承载。
 */
export function PageHeader({ title, headingId, description, actions }: PageHeaderProps) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 id={headingId} className="heading-md">
          {title}
        </h1>
        {description ? <p className="mt-1 max-w-3xl text-sm text-secondary">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  )
}
