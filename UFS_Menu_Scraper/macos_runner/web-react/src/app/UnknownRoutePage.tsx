import { Link, useLocation } from 'react-router-dom'
import { StatePanel } from '../components/StatePanel'

/** 未知路由：说明地址无效，并提供工作台与任务中心两个有效入口。 */
export function UnknownRoutePage() {
  const location = useLocation()
  const hash = location.pathname.replace(/^\//, '')
  return (
    <StatePanel
      variant="info"
      title="页面不存在"
      description={`没有找到“#${hash || 'home'}”对应的页面，可能是链接已过期或地址不完整。`}
      action={
        <div className="flex flex-wrap items-center justify-center gap-4 text-sm">
          <Link to="/workbench" className="text-primary underline-offset-2 hover:underline">
            返回采集工作台
          </Link>
          <Link to="/task-center" className="text-primary underline-offset-2 hover:underline">
            前往任务中心
          </Link>
        </div>
      }
    />
  )
}
