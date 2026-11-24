import { CopyButton } from './CopyButton'

interface CellTextProps {
  /** 只接受字符串：表格单元格统一省略、悬停查看全文，避免长文本撑开列宽。 */
  children: string | null | undefined
  className?: string
  /** ID、路径、错误码等使用等宽字体。 */
  mono?: boolean
  copyable?: boolean
  /** 复制按钮的读屏对象名，默认取内容首段。 */
  copyLabel?: string
  empty?: string
  /** 附加在文本上的最大宽度类，例如 `max-w-40`。 */
  maxWidthClass?: string
}

/**
 * 表格/详情中的单行文本：超出省略并提供 title 查看全文；
 * 需要跨行展示的说明不要使用本组件。
 */
export function CellText({
  children,
  className = '',
  mono = false,
  copyable = false,
  copyLabel,
  empty = '—',
  maxWidthClass = '',
}: CellTextProps) {
  const text = typeof children === 'string' ? children : ''
  if (!text) return <span className="text-tertiary">{empty}</span>
  return (
    <span className={`flex min-w-0 items-center gap-1 ${className}`}>
      <span
        className={`block min-w-0 truncate ${mono ? 'font-mono text-xs' : ''} ${maxWidthClass}`}
        title={text}
      >
        {text}
      </span>
      {copyable ? <CopyButton value={text} label={copyLabel ?? '内容'} className="shrink-0" /> : null}
    </span>
  )
}
