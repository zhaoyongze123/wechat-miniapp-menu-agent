import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { useRole } from '../../app/RoleProvider'
import { isOnline } from '../hub/format'
import { connectSharedWorkbench } from './api'

/**
 * 共享工作台配对：
 * - Hub 展示设备概况并跳转设备管理生成配对码；
 * - Worker 未配对或凭证失效时在这里填写工作台地址、电脑名称与一次性配对码；
 * - 服务端在采集运行中会拒绝配对（409），错误原样展示。
 */
export function SharedWorkbenchCard() {
  const role = useRole()

  if (role.role === 'standalone') return null

  return (
    <div className="space-y-3" aria-labelledby="shared-heading">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 id="shared-heading" className="text-sm text-default">
            共享工作台
          </h3>
          <p className="mt-1 text-sm text-secondary">{summaryText(role)}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={role.role === 'hub' ? 'info' : 'secondary'}>
            {role.role === 'hub' ? '共享后台' : 'Windows 采集端'}
          </StatusBadge>
          <Button color="secondary" variant="outline" size="sm" onClick={role.reload}>
            刷新
          </Button>
          {role.role === 'hub' ? (
            <Link
              to="/devices"
              className="rounded-md border border-subtle px-3 py-1.5 text-sm text-default hover:bg-secondary-ghost-hover"
            >
              管理采集电脑
            </Link>
          ) : null}
        </div>
      </div>

      {role.role === 'worker' ? <WorkerPairing /> : null}

      {role.error ? (
        <Alert
          color="danger"
          variant="soft"
          className="mt-3"
          title="共享工作台状态读取失败"
          description={role.error}
        />
      ) : null}

      {role.role === 'hub' ? (
        <p className="mt-3 text-xs text-tertiary">
          配对码在“采集电脑”页面生成，一次性有效；解除配对或移除设备同样在该页面操作。
        </p>
      ) : null}
    </div>
  )
}

function summaryText(role: ReturnType<typeof useRole>): string {
  const status = role.status
  if (!status) return '正在读取连接状态…'
  if (status.role === 'hub') {
    const online = status.workers.filter((worker) => !worker.revoked_at && !worker.archived_at && isOnline(worker.last_seen)).length
    return `已配对 ${status.workers.length} 台采集电脑，当前在线 ${online} 台。`
  }
  if (status.role === 'worker') {
    if (!status.paired) return '尚未配对共享后台；配对后由后台统一分配门店任务。'
    if (status.credential_invalid) return '配对凭证已失效，请重新输入工作台配对码。'
    if (!status.online) {
      return `共享后台离线${status.pending_uploads ? `，${status.pending_uploads} 个结果待补传` : ''}；恢复连接后会自动补传。`
    }
    return `已连接共享后台${status.name ? ` · ${status.name}` : ''}${
      status.pending_uploads ? `，${status.pending_uploads} 个结果待补传` : ''
    }。${status.notice ? ` ${status.notice}` : ''}`
  }
  return ''
}

function WorkerPairing() {
  const role = useRole()
  const toast = useToast()
  const status = role.status?.role === 'worker' ? role.status : undefined
  const needsPairing = !status?.paired || Boolean(status.credential_invalid)
  const [url, setUrl] = useState('')
  const [name, setName] = useState(status?.name ?? '')
  const [code, setCode] = useState('')
  const [error, setError] = useState('')

  const mutation = useMutation({
    mutationFn: connectSharedWorkbench,
    onSuccess: () => {
      setCode('')
      setError('')
      toast.push(status?.paired ? '已重新配对共享后台' : '已配对共享后台', 'success')
      role.reload()
    },
    onError: (cause) => setError(errorMessage(cause)),
  })

  const submit = () => {
    if (!url.trim() || !name.trim() || !code.trim()) {
      setError('请填写工作台地址、电脑名称和配对码')
      return
    }
    mutation.mutate({ url: url.trim(), name: name.trim(), code: code.trim() })
  }

  if (!needsPairing) {
    return (
      <p className="mt-3 text-xs text-tertiary">
        凭证失效后，此处会显示重新配对表单。
        <button
          type="button"
          className="ml-2 underline underline-offset-2"
          onClick={() => {
            setCode('')
            setError('')
            mutation.reset()
            role.reload()
          }}
        >
          重新读取状态
        </button>
      </p>
    )
  }

  return (
    <div className="mt-4 space-y-3 border-t border-subtle pt-4">
      {status?.credential_invalid ? (
        <Alert
          color="warning"
          variant="soft"
          title="配对凭证已失效"
          description="请向管理员索取新的配对码；已采集但未上传的结果会保留在本机，重新配对后继续补传。"
        />
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1 text-sm text-default">
          <span>工作台地址</span>
          <Input
            type="url"
            value={url}
            placeholder="https://…"
            maxLength={300}
            autoComplete="off"
            onChange={(event) => setUrl(event.target.value)}
          />
        </label>
        <label className="space-y-1 text-sm text-default">
          <span>电脑名称</span>
          <Input
            value={name}
            placeholder="如 采集电脑 B"
            maxLength={80}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label className="space-y-1 text-sm text-default sm:col-span-2">
          <span>配对码</span>
          <Input
            value={code}
            placeholder="向管理员索取一次性配对码"
            maxLength={96}
            autoComplete="one-time-code"
            onChange={(event) => setCode(event.target.value)}
          />
        </label>
      </div>
      {error ? <p className="text-xs text-danger" role="alert">{error}</p> : null}
      <div className="flex justify-end">
        <Button color="primary" size="sm" disabled={mutation.isPending} onClick={submit}>
          {mutation.isPending ? '连接中…' : '连接工作台'}
        </Button>
      </div>
    </div>
  )
}
