import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'
import { ExportPreviewDialog } from './ExportPreviewDialog'

const DEFAULT_QUERY = {
  batchId: 'all',
  source: 'all' as const,
  reason: 'all',
  q: '',
  reviewed: 'all' as const,
}

describe('ExportPreviewDialog', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('展示全量门店名称（含 reason 列），搜索仅用于预览核对且确认导出仍导出全量', async () => {
    vi.spyOn(api, 'fetchStoreNamesExportPreview').mockResolvedValue({
      filename: '全量门店名称.xlsx',
      sheet_name: '全量门店名称',
      columns: [
        { key: 'task_id', label: '原始任务编号', width: 40 },
        { key: 'store_name', label: '原始门店名称', width: 30 },
        { key: 'observed_store_name', label: '实际观察门店名称', width: 30 },
        { key: 'state', label: '采集状态', width: 18 },
        { key: 'reason', label: '原因（reason）', width: 48 },
      ],
      rows: [
        {
          task_id: 'task-1',
          store_name: '门店A',
          observed_store_name: '实际门店A',
          state: '已完成',
          reason: '已进入菜单页，识别门店为实际门店A',
        },
        {
          task_id: 'task-2',
          store_name: '门店B',
          observed_store_name: '',
          state: '待检查',
          reason: '页面顶部显示「门店B临港店」，但未触发结构化输出',
        },
      ],
      total: 2,
      summary: {
        total: 2,
        extracted: 1,
        missing: 1,
        states: { 已完成: 1, 待检查: 1 },
      },
    })

    const onConfirmExport = vi.fn().mockResolvedValue(undefined)
    render(
      <ExportPreviewDialog
        open
        kind="store-names"
        rejectionQuery={DEFAULT_QUERY}
        exporting={false}
        onConfirmExport={onConfirmExport}
        onOpenChange={() => {}}
      />,
    )

    expect(await screen.findByRole('heading', { name: '全量门店名称预览' })).toBeInTheDocument()
    expect(await screen.findByText('原因（reason）')).toBeInTheDocument()
    expect(screen.getByText('页面顶部显示「门店B临港店」，但未触发结构化输出')).toBeInTheDocument()
    expect(screen.getByText('1 / 2（50.0%）')).toBeInTheDocument()

    // 搜索仅用于预览核对，不改变“确认导出 Excel（2 条）”的总数
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '临港店' } })
    await waitFor(() => {
      expect(screen.queryByText('门店A')).not.toBeInTheDocument()
    })
    expect(screen.getByText('门店B')).toBeInTheDocument()

    const confirmBtn = screen.getByRole('button', { name: '确认导出 Excel（2 条）' })
    fireEvent.click(confirmBtn)
    expect(onConfirmExport).toHaveBeenCalledTimes(1)
  })

  it('0 条可导出数据时置灰确认导出按钮', async () => {
    vi.spyOn(api, 'fetchRejectionsExportPreview').mockResolvedValue({
      filename: '不合格门店清单.xlsx',
      sheet_name: '不合格门店',
      columns: [{ key: 'store_name', label: '门店', width: 34 }],
      rows: [],
      total: 0,
      summary: { total: 0, reviewed: 0, unreviewed: 0 },
    })

    render(
      <ExportPreviewDialog
        open
        kind="rejections"
        rejectionQuery={DEFAULT_QUERY}
        exporting={false}
        onConfirmExport={vi.fn().mockResolvedValue(undefined)}
        onOpenChange={() => {}}
      />,
    )

    expect(await screen.findByText('暂无可导出数据')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认导出 Excel（0 条）' })).toBeDisabled()
  })
})
