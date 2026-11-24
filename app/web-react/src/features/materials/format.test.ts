import { describe, expect, it } from 'vitest'
import {
  MAX_BATCH_FILES,
  MAX_FILE_BYTES,
  batchStatusMeta,
  decisionMeta,
  formatBytes,
  formatDuration,
  isJobActive,
  isJobOpen,
  normalizeDecision,
  parseStoreKey,
  relativePathOf,
  storeKey,
  validateBatchFiles,
} from './format'

function fakeFile(name: string, size: number): File {
  return { name, size } as File
}

describe('素材状态口径', () => {
  it('旧数据 rejected 归一化为 skipped，其他值回到 pending', () => {
    expect(normalizeDecision('rejected')).toBe('skipped')
    expect(normalizeDecision(undefined)).toBe('pending')
    expect(normalizeDecision('unknown')).toBe('pending')
    expect(decisionMeta('confirmed').label).toBe('已确认')
    expect(decisionMeta('skipped').tone).toBe('danger')
  })

  it('批次与任务状态互不混用，未知状态原样展示', () => {
    expect(batchStatusMeta('ready_to_analyze').label).toBe('待分析')
    expect(batchStatusMeta('paused').label).toBe('paused')
    expect(isJobOpen('paused')).toBe(true)
    expect(isJobActive('paused')).toBe(false)
    expect(isJobOpen('completed')).toBe(false)
  })
})

describe('上传预检', () => {
  it('空选择与不支持的扩展名会被拒绝', () => {
    expect(validateBatchFiles([])).toBe('请选择图片文件或素材文件夹')
    expect(validateBatchFiles([fakeFile('a.txt', 10)])).toContain('仅支持')
  })

  it('单文件与文件数量上限与后端一致', () => {
    expect(validateBatchFiles([fakeFile('a.jpg', MAX_FILE_BYTES + 1)])).toContain('单文件')
    const many = Array.from({ length: MAX_BATCH_FILES + 1 }, (_, index) => fakeFile(`${index}.png`, 1))
    expect(validateBatchFiles(many)).toContain('最多')
  })

  it('合法图片通过并支持目录相对路径', () => {
    expect(validateBatchFiles([fakeFile('a.JPG', 1024)])).toBeNull()
    expect(relativePathOf({ name: 'a.jpg', size: 1, webkitRelativePath: '门店/a.jpg' } as File)).toBe('门店/a.jpg')
    expect(relativePathOf(fakeFile('a.jpg', 1))).toBe('a.jpg')
  })
})

describe('展示辅助', () => {
  it('字节与时长格式化', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatDuration(45)).toBe('45 秒')
    expect(formatDuration(125)).toBe('2 分 5 秒')
    expect(formatDuration(undefined)).toBe('—')
  })

  it('返工门店键可往返解析', () => {
    const key = storeKey('batch-1', 'store-9')
    expect(key).toBe('batch-1:store-9')
    expect(parseStoreKey(key)).toEqual({ batchId: 'batch-1', storeId: 'store-9' })
  })
})
