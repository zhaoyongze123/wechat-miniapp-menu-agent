import { Button } from '@openai/apps-sdk-ui/components/Button'
import { StatePanel } from '../../components/StatePanel'
import { useRole } from '../../app/RoleProvider'
import { HubWorkbenchPage } from '../hub/HubWorkbenchPage'
import { WorkerWorkbenchPage } from './WorkerWorkbenchPage'

/**
 * 同一路径按角色分流：Hub 负责发布门店并分配给采集电脑，
 * Windows 采集端负责本机队列、全自动/半自动与断线续传。
 */
export function WorkbenchRoute() {
  const { role, loading, error, reload } = useRole()
  if (loading) return <StatePanel variant="loading" title="正在确认运行角色…" />
  if (error) {
    return (
      <StatePanel
        variant="error"
        title="无法确认运行角色"
        description={error}
        action={
          <Button color="secondary" variant="outline" size="sm" onClick={reload}>
            重试
          </Button>
        }
      />
    )
  }
  if (role === 'hub') return <HubWorkbenchPage />
  if (role === 'worker') return <WorkerWorkbenchPage />
  return (
    <StatePanel
      variant="info"
      title="独立模式没有采集工作台"
      description="独立模式用于本机整理素材与审核，不参与共享分配；如需采集请在采集端启动。"
    />
  )
}
