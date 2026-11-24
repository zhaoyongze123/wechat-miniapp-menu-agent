import type { ReactNode } from 'react'
import { Button } from '@openai/apps-sdk-ui/components/Button'

export interface SettingsNavItem {
  id: string
  label: string
  /** 可选的一行范围说明，仅在分类导航中展示。 */
  description?: string
  /** 需要主动提醒时显示，例如“有新版本”。 */
  badge?: string
}

interface SettingsLayoutProps {
  items: SettingsNavItem[]
  activeId: string
  onSelect: (id: string) => void
  children: ReactNode
  ariaLabel?: string
}

/**
 * 设置中心骨架：桌面为左侧分类导航 + 右侧内容，窄屏改为顶部横向导航。
 * 只负责布局与选中态，不保存状态；切换分类由调用方决定是否拦截未保存修改。
 */
export function SettingsLayout({ items, activeId, onSelect, children, ariaLabel = '设置分类' }: SettingsLayoutProps) {
  return (
    <div className="grid gap-6 lg:grid-cols-[11rem_minmax(0,1fr)]">
      <nav aria-label={ariaLabel} className="min-w-0">
        <ul className="flex gap-1 overflow-x-auto pb-1 lg:sticky lg:top-0 lg:flex-col lg:overflow-visible lg:pb-0">
          {items.map((item) => {
            const active = item.id === activeId
            return (
              <li key={item.id} className="shrink-0 lg:w-full">
                <button
                  type="button"
                  onClick={() => onSelect(item.id)}
                  aria-current={active ? 'true' : undefined}
                  className={[
                    'w-full rounded-lg px-3 py-2 text-left text-sm transition-colors',
                    active
                      ? 'bg-secondary-soft text-default'
                      : 'text-secondary hover:bg-secondary-ghost-hover hover:text-default',
                  ].join(' ')}
                >
                  <span className="block whitespace-nowrap lg:whitespace-normal">
                    {item.label}
                    {item.badge ? (
                      <span className="ml-2 rounded-full border border-subtle bg-surface-secondary px-1.5 py-0.5 text-2xs text-warning">{item.badge}</span>
                    ) : null}
                  </span>
                  {item.description ? (
                    <span className="mt-0.5 hidden text-2xs text-tertiary lg:block">{item.description}</span>
                  ) : null}
                </button>
              </li>
            )
          })}
        </ul>
      </nav>
      <div className="min-w-0 space-y-6">{children}</div>
    </div>
  )
}

interface SettingSectionProps {
  id: string
  title: string
  description?: string
  children: ReactNode
}

/** 单个设置分区：标题 + 可选说明 + 行内容，锚点用于分类定位。 */
export function SettingSection({ id, title, description, children }: SettingSectionProps) {
  return (
    <section
      id={id}
      aria-labelledby={`${id}-heading`}
      className="scroll-mt-4 space-y-4 rounded-xl border border-subtle bg-surface p-4"
    >
      <div>
        <h2 id={`${id}-heading`} className="heading-xs">
          {title}
        </h2>
        {description ? <p className="mt-1 text-xs text-secondary">{description}</p> : null}
      </div>
      {children}
    </section>
  )
}

interface SettingRowProps {
  label: string
  description?: string
  /** 表单控件 id；提供后标签与控件建立真实关联。 */
  htmlFor?: string
  children: ReactNode
  /** 控件区更宽（如目录路径、错误详情）时使用。 */
  wide?: boolean
}

/** 一行设置：左侧标题/说明，右侧控件；窄屏上下排列。 */
export function SettingRow({ label, description, htmlFor, children, wide = false }: SettingRowProps) {
  const LabelTag = htmlFor ? 'label' : 'span'
  return (
    <div
      className={[
        'grid gap-2 sm:gap-x-6',
        wide
          ? 'sm:grid-cols-[minmax(0,1fr)_minmax(0,28rem)]'
          : 'sm:grid-cols-[minmax(0,1fr)_minmax(0,20rem)]',
        'sm:items-start',
      ].join(' ')}
    >
      <div className="min-w-0">
        <LabelTag htmlFor={htmlFor} className="block text-sm text-default">
          {label}
        </LabelTag>
        {description ? <p className="mt-1 text-xs leading-relaxed text-secondary">{description}</p> : null}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  )
}

interface FormFieldProps {
  label: string
  htmlFor?: string
  help?: string
  error?: string
  full?: boolean
  children: ReactNode
}

/** 表单字段：标签、帮助文本与行内错误；错误优先展示并保留输入。 */
export function FormField({ label, htmlFor, help, error, full = false, children }: FormFieldProps) {
  return (
    <div className={full ? 'space-y-1 sm:col-span-2' : 'space-y-1'}>
      <label htmlFor={htmlFor} className="block text-sm text-default">
        {label}
      </label>
      {children}
      {error ? (
        <p className="text-2xs text-danger" role="alert">
          {error}
        </p>
      ) : help ? (
        <p className="text-2xs text-tertiary">{help}</p>
      ) : null}
    </div>
  )
}

interface SettingsSaveBarProps {
  dirty: boolean
  saving: boolean
  error?: string
  note?: string
  onReset: () => void
}

/**
 * 服务端配置保存栏：吸底常显，明确区分已保存/未保存/保存中/失败。
 * 作为表单内元素使用，主按钮触发 submit，避免调用方各自实现防重。
 */
export function SettingsSaveBar({ dirty, saving, error, note, onReset }: SettingsSaveBarProps) {
  return (
    <div className="sticky bottom-0 z-10 flex flex-wrap items-center gap-3 border-t border-subtle bg-surface/95 px-1 py-3 backdrop-blur">
      <Button type="submit" color="primary" size="sm" disabled={!dirty || saving}>
        {saving ? '保存中…' : dirty ? '保存修改' : '已保存'}
      </Button>
      {dirty ? (
        <Button type="button" color="secondary" variant="ghost" size="sm" disabled={saving} onClick={onReset}>
          放弃修改
        </Button>
      ) : null}
      {error ? (
        <span className="text-xs text-danger" role="alert">
          {error}
        </span>
      ) : (
        <span className="text-2xs text-tertiary">{dirty ? '有未保存的修改，离开前请保存或放弃。' : note}</span>
      )}
    </div>
  )
}
