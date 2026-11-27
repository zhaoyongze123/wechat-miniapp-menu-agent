import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Badge } from '@openai/apps-sdk-ui/components/Badge'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { errorMessage } from '../../api/client'
import { StatusBadge } from '../../components/StatusBadge'
import { formatStore, manualStageLabel, REJECTION_REASON_OPTIONS } from './format'
import type { ManualRecognizeStoreNameResponse, ManualState } from './types'

interface ManualPanelProps {
  manual: ManualState
  busy: boolean
  error: string
  onCapture: () => void
  onReject: (reason: string, detail: string) => void
  onSkip?: () => void
  onRecognizeStoreName?: () => Promise<ManualRecognizeStoreNameResponse>
  onSaveStoreName?: (storeName: string) => Promise<void>
  extraSettings?: ReactNode
  compact?: boolean
}

/**
 * 半自动人工采集辅助面板：
 * - 独立辅助能力：AI 识别实际门店名称并支持人工微调保存，不阻塞原有截图/不合格主流程；
 * - 快捷键：F7 AI 识别、F8 开始截图、F9 标记不合格。
 */
export function ManualPanel({
  manual,
  busy,
  error,
  onCapture,
  onReject,
  onSkip,
  onRecognizeStoreName,
  onSaveStoreName,
  extraSettings,
  compact = false,
}: ManualPanelProps) {
  const panel = useRef<HTMLElement>(null)
  const [reason, setReason] = useState(manual.reason || 'blur')
  const [detail, setDetail] = useState('')
  const [rejectOpen, setRejectOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [shortcutEnabled, setShortcutEnabled] = useState(
    () => window.localStorage.getItem('menu-capture.manual-shortcuts') !== 'off',
  )

  const [storeNameInput, setStoreNameInput] = useState(manual.observed_store_name ?? '')
  const [savedStoreName, setSavedStoreName] = useState(manual.observed_store_name ?? '')
  const [recognizing, setRecognizing] = useState(false)
  const [savingName, setSavingName] = useState(false)
  const [nameFeedback, setNameFeedback] = useState('')
  const [nameError, setNameError] = useState('')
  const activeTaskRef = useRef(manual.task_id)

  useEffect(() => {
    activeTaskRef.current = manual.task_id
    setDetail('')
    setRejectOpen(false)
    setRecognizing(false)
    setSavingName(false)
    setNameFeedback('')
    setNameError('')
    const initial = manual.observed_store_name ?? ''
    setStoreNameInput(initial)
    setSavedStoreName(initial)
    if (manual.reason) setReason(manual.reason)
  }, [manual.task_id, manual.wait_id])

  useEffect(() => {
    if (manual.reason) setReason(manual.reason)
  }, [manual.reason])

  useEffect(() => {
    const incoming = manual.observed_store_name ?? ''
    if (incoming && incoming !== savedStoreName && storeNameInput === savedStoreName) {
      setStoreNameInput(incoming)
      setSavedStoreName(incoming)
    }
  }, [manual.observed_store_name, savedStoreName, storeNameInput])

  const triggerRecognize = async () => {
    if (!onRecognizeStoreName || recognizing || !manual.waiting) return
    const taskIdAtStart = manual.task_id
    setRecognizing(true)
    setNameError('')
    setNameFeedback('')
    try {
      const result = await onRecognizeStoreName()
      if (activeTaskRef.current !== taskIdAtStart || result.stale) return
      const recognized = (result.recognized_name ?? result.observed_store_name ?? '').trim()
      if (recognized) {
        setStoreNameInput(recognized)
        setSavedStoreName(recognized)
        setNameFeedback(result.reason ? `已识别并保存 · ${result.reason}` : '已识别并保存')
      } else {
        setNameFeedback(result.reason || '未在当前画面发现明确门店名，可手动输入')
      }
    } catch (err) {
      if (activeTaskRef.current !== taskIdAtStart) return
      setNameError(errorMessage(err))
    } finally {
      if (activeTaskRef.current === taskIdAtStart) {
        setRecognizing(false)
      }
    }
  }

  const commitStoreName = async (nextValue: string) => {
    if (!onSaveStoreName || savingName) return
    const cleaned = nextValue.trim()
    if (cleaned === savedStoreName) return
    const taskIdAtStart = manual.task_id
    setSavingName(true)
    setNameError('')
    try {
      await onSaveStoreName(cleaned)
      if (activeTaskRef.current !== taskIdAtStart) return
      setStoreNameInput(cleaned)
      setSavedStoreName(cleaned)
      setNameFeedback(cleaned ? '已保存实际门店名称' : '已清空实际门店名称')
    } catch (err) {
      if (activeTaskRef.current !== taskIdAtStart) return
      setNameError(errorMessage(err))
    } finally {
      if (activeTaskRef.current === taskIdAtStart) {
        setSavingName(false)
      }
    }
  }

  const handleCapture = () => {
    if (onSaveStoreName && storeNameInput.trim() !== savedStoreName && !savingName) {
      void commitStoreName(storeNameInput).finally(() => onCapture())
      return
    }
    onCapture()
  }

  const handleSubmitReject = () => {
    const trimmedDetail = detail.trim()
    if (onSaveStoreName && storeNameInput.trim() !== savedStoreName && !savingName) {
      void commitStoreName(storeNameInput).finally(() => onReject(reason, trimmedDetail))
      return
    }
    onReject(reason, trimmedDetail)
  }

  useEffect(() => {
    const ownerDocument = panel.current?.ownerDocument
    if (!shortcutEnabled || !manual.waiting || !ownerDocument) return
    const handler = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (
        event.repeat
        || event.altKey
        || event.ctrlKey
        || event.metaKey
        || target?.closest?.('input,textarea,select,[contenteditable],[role=combobox]')
        || rejectOpen
      ) {
        return
      }
      if (event.key === 'F7' && onRecognizeStoreName) {
        event.preventDefault()
        void triggerRecognize()
      }
      if (event.key === 'F8') {
        event.preventDefault()
        if (!busy) handleCapture()
      }
      if (event.key === 'F9') {
        event.preventDefault()
        setRejectOpen(true)
      }
    }
    ownerDocument.addEventListener('keydown', handler)
    return () => ownerDocument.removeEventListener('keydown', handler)
  })

  const store = formatStore(manual.store_name)
  const needDetail = reason === 'other'
  const progressPercent = manual.total > 0 ? Math.min(100, Math.round((manual.index / manual.total) * 100)) : 0

  return (
    <section
      ref={panel}
      className="space-y-3 rounded-xl border border-subtle bg-surface p-3.5 shadow-[var(--shadow-100)]"
      aria-label="半自动采集辅助"
    >
      {/* 顶栏：紧凑进度 + 状态徽标 + 设置收纳 */}
      <div className="space-y-2">
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <span className="text-xs font-medium text-default">
              半自动采集 · {manual.index} / {manual.total}
            </span>
            <StatusBadge tone={manual.waiting ? 'warning' : 'info'}>
              {manual.waiting ? '等待操作' : manualStageLabel(manual)}
            </StatusBadge>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {!compact ? (
              <a
                className="rounded-md px-2 py-1 text-xs text-secondary underline-offset-2 hover:bg-surface-secondary hover:text-default hover:underline"
                href="/remote-control"
                target="menu-capture-manual-control"
              >
                置顶浮窗
              </a>
            ) : null}
            <Button
              color="secondary"
              variant="ghost"
              size="sm"
              aria-expanded={settingsOpen}
              aria-label="辅助设置"
              onClick={() => setSettingsOpen((open) => !open)}
            >
              设置
            </Button>
          </div>
        </div>
        <div className="h-1 w-full overflow-hidden rounded-full bg-surface-secondary" aria-hidden="true">
          <div
            className="h-full rounded-full bg-[var(--color-text-primary)] transition-all duration-300"
            style={{ width: `${progressPercent}%` }}
          />
        </div>
      </div>

      {/* 收纳的低频设置区 */}
      {settingsOpen ? (
        <div className="space-y-2.5 rounded-lg border border-subtle bg-surface-secondary p-3 text-xs">
          <Checkbox
            checked={shortcutEnabled}
            onCheckedChange={(checked) => {
              const enabled = Boolean(checked)
              setShortcutEnabled(enabled)
              window.localStorage.setItem('menu-capture.manual-shortcuts', enabled ? 'on' : 'off')
            }}
            label="启用键盘快捷键（F7 AI 识别 · F8 截图 · F9 不合格）"
          />
          {extraSettings}
        </div>
      ) : null}

      {/* 当前门店核心卡片 + 独立 AI 门店名称识别区 */}
      <div className="space-y-2.5 rounded-lg border border-subtle bg-surface-secondary/60 p-3">
        <div className="flex items-start gap-2">
          {store.code ? (
            <Badge color="secondary" variant="soft" size="sm" className="mt-0.5 shrink-0 font-mono">
              {store.code}
            </Badge>
          ) : null}
          <h2 className="heading-xs min-w-0 flex-1 break-words text-default">
            {store.name || manual.store_name}
          </h2>
        </div>

        {(onRecognizeStoreName || onSaveStoreName) ? (
          <div className="space-y-1.5 pt-1">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-medium text-secondary">实际门店名称</span>
              {savingName ? <span className="text-2xs text-tertiary">保存中…</span> : null}
            </div>
            <div className="flex items-center gap-2">
              <div className="min-w-0 flex-1">
                <Input
                  size="sm"
                  aria-label="实际门店名称"
                  placeholder="点击右侧 AI 识别，或手动输入"
                  value={storeNameInput}
                  maxLength={40}
                  disabled={!manual.waiting || savingName}
                  onChange={(event) => {
                    setStoreNameInput(event.target.value)
                    if (nameError) setNameError('')
                  }}
                  onBlur={() => void commitStoreName(storeNameInput)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      event.preventDefault()
                      void commitStoreName(storeNameInput)
                    }
                  }}
                />
              </div>
              {onRecognizeStoreName ? (
                <Button
                  color="secondary"
                  variant="outline"
                  size="sm"
                  disabled={!manual.waiting || recognizing}
                  onClick={() => void triggerRecognize()}
                >
                  {recognizing ? '识别中…' : 'AI 识别'}
                  {shortcutEnabled && !recognizing ? <kbd className="ml-1 text-2xs opacity-60">F7</kbd> : null}
                </Button>
              ) : null}
            </div>
            {nameError ? (
              <p className="text-2xs text-danger" role="alert">
                {nameError}
              </p>
            ) : nameFeedback ? (
              <p className="truncate text-2xs text-secondary" title={nameFeedback} role="status">
                {nameFeedback}
              </p>
            ) : null}
          </div>
        ) : null}
      </div>

      {/* 高频动作区：主操作整行宽 + 次级双列 */}
      {manual.waiting ? (
        <div className="space-y-2">
          <Button color="primary" size="md" block disabled={busy} onClick={handleCapture}>
            开始截图 {shortcutEnabled ? <kbd className="ml-1.5 text-xs opacity-75">F8</kbd> : null}
          </Button>
          <div className={`grid gap-2 ${onSkip ? 'grid-cols-2' : 'grid-cols-1'}`}>
            <Button
              color="danger"
              variant="outline"
              size="sm"
              block
              disabled={busy}
              onClick={() => setRejectOpen((open) => !open)}
            >
              标记不合格 {shortcutEnabled ? <kbd className="ml-1 text-2xs opacity-70">F9</kbd> : null}
            </Button>
            {onSkip ? (
              <Button color="secondary" variant="outline" size="sm" block disabled={busy} onClick={onSkip}>
                跳过本店
              </Button>
            ) : null}
          </div>

          {rejectOpen ? (
            <div className="space-y-2 rounded-lg border border-subtle bg-surface-secondary/50 p-3">
              <div className="space-y-2">
                <Select
                  aria-label="不合格原因"
                  value={reason}
                  options={REJECTION_REASON_OPTIONS}
                  size="sm"
                  onChange={(option) => setReason(option.value)}
                />
                <Input
                  size="sm"
                  aria-label="补充说明"
                  placeholder={needDetail ? '请补充具体原因（必填）' : '补充说明（可选）'}
                  value={detail}
                  maxLength={200}
                  onChange={(event) => setDetail(event.target.value)}
                />
              </div>
              <div className="flex justify-end gap-2">
                <Button color="secondary" variant="ghost" size="sm" onClick={() => setRejectOpen(false)}>
                  取消
                </Button>
                <Button
                  color="danger"
                  size="sm"
                  disabled={busy || (needDetail && !detail.trim())}
                  onClick={handleSubmitReject}
                >
                  提交不合格
                </Button>
              </div>
            </div>
          ) : null}
        </div>
      ) : (
        <p className="rounded-lg bg-surface-secondary px-3 py-2 text-center text-xs text-secondary">
          正在处理本店结果，请稍候…
        </p>
      )}

      {manual.last_result ? (
        <p className="truncate border-t border-subtle pt-2 text-2xs text-secondary" title={manual.last_result}>
          {manual.last_result}
        </p>
      ) : null}

      {error ? <Alert color="danger" variant="soft" title={error} /> : null}
    </section>
  )
}
