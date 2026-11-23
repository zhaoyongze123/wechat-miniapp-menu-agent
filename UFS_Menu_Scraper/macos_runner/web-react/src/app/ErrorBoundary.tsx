import { Component, type ErrorInfo, type ReactNode } from 'react'
import { StatePanel } from '../components/StatePanel'

interface State {
  error: Error | null
}

/** 页面级错误边界：渲染崩溃时保留外壳，给出重试入口而不是白屏。 */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  override state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('React 页面渲染失败', error, info.componentStack)
  }

  override render(): ReactNode {
    if (this.state.error) {
      return (
        <StatePanel
          variant="error"
          title="页面渲染失败"
          description={this.state.error.message}
          action={
            <button
              type="button"
              className="rounded-md border border-shell-line px-3 py-1.5 text-sm hover:border-accent-400"
              onClick={() => this.setState({ error: null })}
            >
              重试
            </button>
          }
        />
      )
    }
    return this.props.children
  }
}
