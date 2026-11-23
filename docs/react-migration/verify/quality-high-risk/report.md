# 质量审核高风险补测（隔离环境）

- 结果：46/46 通过

- [PASS] export:all
- [PASS] export:source-capture
- [PASS] export:source-materials-empty
- [PASS] export:reason-blur-empty
- [PASS] export:query-sh010
- [PASS] export:query-none
- [PASS] export:batch-hit
- [PASS] export:batch-miss
- [PASS] export:combined-empty
- [PASS] export:invalid-reason
- [PASS] hub:no-auth — Hub 未带管理员认证
- [PASS] hub:write-without-token — Hub 写操作缺本机令牌
- [PASS] worker:read-rejections — Worker 允许只读查看
- [PASS] worker:review-rejections — Worker 不能审核
- [PASS] worker:retry — Worker 不能重试采集
- [PASS] worker:create-batch — Worker 不能建素材批次
- [PASS] worker:undo-material-rejection — Worker 不能撤销素材不合格
- [PASS] worker:status — Worker 状态角色
- [PASS] worker:assign-endpoint — Worker 无 Hub 分配接口，且写操作先被本机令牌拦截
- [PASS] return:before-rejections
- [PASS] return:submit — 打回素材清洗
- [PASS] return:material-decision
- [PASS] return:assign-blocked — 打回后单店分配应被阻塞
- [PASS] return:retry-blocked — 待重新选码时不能重试采集
- [PASS] return:history-event
- [PASS] return:task-still-listed
- [PASS] return:run-history
- [PASS] material:mark-skip — 临时把已打回门店标为素材不合格
- [PASS] export:materials-one
- [PASS] export:reason-blur-one
- [PASS] export:materials-query-hit
- [PASS] export:materials-query-miss
- [PASS] export:all-two
- [PASS] material:undo — 撤销素材不合格回到待处理
- [PASS] export:materials-empty-again
- [PASS] capture:undo-before
- [PASS] capture:undo — 撤销采集不合格
- [PASS] export:capture-empty-after-undo
- [PASS] capture:retry-rejected-status — 观察：rejected 状态撤销后仍不可 /retry，需走打回素材或重新发布
- [PASS] capture:retry — 重新采集（重新排队）
- [PASS] capture:retry-state
- [PASS] assign:occupied-conflict — 占用中的门店不能重复分配
- [PASS] assign:submit — 真实分配提交
- [PASS] assign:visible
- [PASS] assign:reclaim — 收回补测分配，恢复环境
- [PASS] assign:reclaimed-state

## 前后状态

```json
{
  "passed": 46,
  "total": 46,
  "failed": [],
  "before": {
    "rejections": {
      "stores": 2,
      "records": 2,
      "materials": 0,
      "capture": 2,
      "reviewed": 0
    },
    "batch_summary": {
      "total_stores": 128,
      "total_images": 128,
      "selected_stores": 128,
      "review_stores": 0,
      "confirmed_stores": 128,
      "skipped_stores": 0,
      "pending_stores": 0,
      "detection_stats": {
        "square": {
          "scanned_stores": 0,
          "hit_stores": 0,
          "ms": 0.0
        },
        "total_ms": 0.0
      }
    },
    "active_assignments": 5
  },
  "after": {
    "rejections": {
      "stores": 0,
      "records": 0,
      "materials": 0,
      "capture": 0,
      "reviewed": 0
    },
    "batch_summary": {
      "total_stores": 128,
      "total_images": 128,
      "selected_stores": 128,
      "review_stores": 0,
      "confirmed_stores": 127,
      "skipped_stores": 0,
      "pending_stores": 1,
      "detection_stats": {
        "square": {
          "scanned_stores": 0,
          "hit_stores": 0,
          "ms": 0.0
        },
        "total_ms": 0.0
      }
    },
    "pending": {
      "stores": 1,
      "batches": 1,
      "needs_review": 1,
      "retryable": 1
    },
    "active_assignments": 5
  }
}
```