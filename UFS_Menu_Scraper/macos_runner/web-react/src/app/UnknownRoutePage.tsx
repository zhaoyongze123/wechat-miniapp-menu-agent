import { useLocation } from 'react-router-dom'
import { StatePanel } from '../components/StatePanel'

/** 未迁移的 hash：说明当前预览范围，并给出回到旧版对应页面的入口。 */
export function UnknownRoutePage() {
  const location = useLocation()
  const hash = location.pathname.replace(/^\//, '')
  return (
    <StatePanel
      variant="info"
      title="该页面尚未迁移到 React"
      description={`“#${hash || 'home'}” 仍由旧版控制台承载。本次迁移先完成任务中心试点，其余模块按阶段推进。`}
      action={
        <div className="flex flex-wrap items-center justify-center gap-3 text-sm">
          <a className="text-accent-300 underline underline-offset-4" href={`/#${hash || 'overview'}`}>
            打开旧版对应页面
          </a>
          <a className="text-accent-300 underline underline-offset-4" href="#/task-center">
            前往 React 任务中心
          </a>
        </div>
      }
    />
  )
}
