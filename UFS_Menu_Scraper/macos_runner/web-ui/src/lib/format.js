export function formatTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  })
}

// 素材事件时间只显示时分秒，日期已由标题行承担。
export function shortTime(value) {
  if (!value) return ''
  const text = String(value)
  return text.length >= 19 ? text.slice(11, 19) : text
}
