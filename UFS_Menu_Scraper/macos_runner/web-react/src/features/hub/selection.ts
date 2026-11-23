/** Hub 一次分配上限与服务端 POST /api/shared/assignments 的校验保持一致。 */
export const HUB_SELECTION_LIMIT = 300

export interface SelectionAddResult {
  ids: string[]
  /** 因超过上限而未加入的门店数量，用于明确提示而不是静默丢弃。 */
  overflow: number
}

/** 跨页累加选择：保持既有顺序、自动去重，并在达到上限时停止。 */
export function addSelection(current: string[], candidates: string[], limit = HUB_SELECTION_LIMIT): SelectionAddResult {
  const ids = [...current]
  const set = new Set(current)
  let overflow = 0
  for (const id of candidates) {
    if (!id || set.has(id)) continue
    if (set.size >= limit) {
      overflow += 1
      continue
    }
    set.add(id)
    ids.push(id)
  }
  return { ids, overflow }
}

/** 从选择中移除指定门店（取消勾选、清空本页或剔除冲突门店共用）。 */
export function removeSelection(current: string[], removed: Iterable<string>): string[] {
  const drop = new Set(removed)
  return current.filter((id) => !drop.has(id))
}
