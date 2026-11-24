import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { CellText } from './CellText'

describe('CellText', () => {
  it('长文本省略并在 title 中保留完整内容', () => {
    const long = '佛山市示例酒店（祖庙路店）· 三层中餐厅东区'
    render(<CellText>{long}</CellText>)
    const node = screen.getByTitle(long)
    expect(node.className).toContain('truncate')
    expect(node).toHaveTextContent(long)
  })

  it('空值显示占位符而不渲染空节点', () => {
    render(<CellText>{null}</CellText>)
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('可复制时提供带对象名的复制按钮', () => {
    render(
      <CellText copyable copyLabel="任务 ID">
        abc123
      </CellText>,
    )
    expect(screen.getByRole('button', { name: '复制任务 ID' })).toBeInTheDocument()
  })
})
