import { useEffect, useRef, useState } from 'react'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { copyText } from '../lib/clipboard'

interface CopyButtonProps {
  value: string
  /** 用于读屏与悬停提示的对象名，例如“门店编号”“任务 ID”。 */
  label?: string
  className?: string
  disabled?: boolean
}

/** 复制按钮：成功/失败都在按钮内反馈，并通过 aria-live 播报，不额外弹 Toast。 */
export function CopyButton({ value, label = '内容', className = '', disabled }: CopyButtonProps) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef(0)

  useEffect(() => () => window.clearTimeout(timer.current), [])

  const onCopy = async () => {
    const ok = await copyText(value)
    setState(ok ? 'copied' : 'failed')
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setState('idle'), 2000)
  }

  const text = state === 'copied' ? '已复制' : state === 'failed' ? '复制失败' : '复制'

  return (
    <span className={`inline-flex items-center ${className}`}>
      <Button
        type="button"
        color="secondary"
        variant="ghost"
        size="sm"
        disabled={disabled || !value}
        aria-label={`复制${label}`}
        title={state === 'failed' ? '浏览器拒绝访问剪贴板，请手动选择文本复制' : `复制${label}`}
        onClick={() => void onCopy()}
      >
        {text}
      </Button>
      <span role="status" aria-live="polite" className="sr-only">
        {state === 'copied' ? `${label}已复制` : state === 'failed' ? `${label}复制失败` : ''}
      </span>
    </span>
  )
}
