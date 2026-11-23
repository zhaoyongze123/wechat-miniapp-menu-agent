import { useEffect } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { AppDialog } from '../../components/AppDialog'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { createPairingCode } from './api'
import { formatTime } from './format'

interface PairingDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/** 配对码由服务端生成并限时有效；窗口关闭后不缓存，避免展示过期代码。 */
export function PairingDialog({ open, onOpenChange }: PairingDialogProps) {
  const toast = useToast()
  const mutation = useMutation({ mutationFn: createPairingCode })

  useEffect(() => {
    if (open) mutation.mutate()
    // 只在打开时取新码；mutation 实例引用稳定。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const copy = async () => {
    const code = mutation.data?.code
    if (!code) return
    try {
      await navigator.clipboard.writeText(code)
      toast.push('配对码已复制', 'success')
    } catch {
      toast.push('浏览器未授权剪贴板，请手动抄写配对码', 'info')
    }
  }

  return (
    <AppDialog
      open={open}
      onOpenChange={onOpenChange}
      title="配对新的采集电脑"
      widthClass="w-[min(560px,94vw)]"
      footer={
        <div className="flex justify-end gap-2">
          <Button color="secondary" variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            关闭
          </Button>
          <Button color="secondary" variant="outline" size="sm" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
            换一个配对码
          </Button>
          <Button color="primary" size="sm" disabled={!mutation.data?.code} onClick={() => void copy()}>
            复制配对码
          </Button>
        </div>
      }
    >
      <div className="space-y-4">
        {mutation.isError ? (
          <Alert
            color="danger"
            variant="soft"
            title="配对码生成失败"
            description={errorMessage(mutation.error)}
          />
        ) : null}

        <div className="rounded-xl border border-subtle bg-surface-secondary px-4 py-6 text-center">
          {mutation.isPending || !mutation.data ? (
            <p className="text-sm text-secondary">正在生成配对码…</p>
          ) : (
            <>
              <p className="font-mono text-3xl tracking-[0.35em] text-default" aria-live="polite">
                {mutation.data.code}
              </p>
              <p className="mt-2 text-xs text-secondary">有效期至 {formatTime(mutation.data.expires_at)}</p>
            </>
          )}
        </div>

        <ol className="space-y-1.5 text-sm text-secondary">
          <li>1. 在 Windows 采集端打开“共享后台配对”。</li>
          <li>2. 输入上面的配对码并提交。</li>
          <li>3. 保持采集端在线，设备状态会自动更新。</li>
        </ol>

        <p className="text-xs text-tertiary">
          配对码只能使用一次；关闭窗口后不再显示，需要时请重新生成。
        </p>
      </div>
    </AppDialog>
  )
}
