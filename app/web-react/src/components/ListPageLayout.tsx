import type { ReactNode } from 'react'
import { PageHeader } from './PageHeader'

interface ListPageLayoutProps {
  /** 页面职责，与 PageHeader 一致。 */
  title: string
  headingId: string
  description?: string
  /** 页面级操作，例如刷新、导入、导出。 */
  actions?: ReactNode
  /** 指标或状态概览，放在标题与筛选之间。 */
  summary?: ReactNode
  /** 筛选工具条（FilterToolbar）。 */
  filters?: ReactNode
  /** 批量操作条（BulkActionBar）；无选择时组件自身返回 null。 */
  bulk?: ReactNode
  /** 页面级错误提示。 */
  error?: ReactNode
  /** 表格或列表主体。 */
  children: ReactNode
  /** 列表区标题；提供时以 h2 呈现，保持「页面 h1 → 列表 h2」的层级。 */
  listTitle?: string
  listTitleId?: string
  /** 分页与结果统计。 */
  pagination?: ReactNode
  /** 表格次要说明，例如选择范围或刷新时间。 */
  footnote?: ReactNode
}

/**
 * 列表页模板：固定「标题 → 概览 → 筛选 → 批量条 → 列表 → 分页」的信息层级与间距。
 *
 * 只统一结构与节奏，不接管业务状态；各页面仍自行管理查询、选择与权限。
 * 新增列表页应优先复用本模板，避免每页重写标题层级与外层容器。
 */
export function ListPageLayout({
  title,
  headingId,
  description,
  actions,
  summary,
  filters,
  bulk,
  error,
  children,
  listTitle,
  listTitleId,
  pagination,
  footnote,
}: ListPageLayoutProps) {
  return (
    <section className="space-y-6" aria-labelledby={headingId}>
      <PageHeader title={title} headingId={headingId} {...(description ? { description } : {})} {...(actions ? { actions } : {})} />
      {summary ? <div className="space-y-3">{summary}</div> : null}
      {error ? <div role="alert">{error}</div> : null}
      {filters || bulk ? (
        <div className="space-y-3">
          {filters}
          {bulk}
        </div>
      ) : null}
      <div className="space-y-3">
        {listTitle ? (
          <h2 id={listTitleId} className="heading-xs">
            {listTitle}
          </h2>
        ) : null}
        {children}
      </div>
      {footnote ? <p className="text-2xs text-tertiary">{footnote}</p> : null}
      {pagination ? <div className="flex flex-wrap items-center justify-between gap-3">{pagination}</div> : null}
    </section>
  )
}
