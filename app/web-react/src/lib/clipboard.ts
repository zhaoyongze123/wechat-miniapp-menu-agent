/**
 * 内网 HTTP 页面通常不是安全上下文，navigator.clipboard 会不可用。
 * 统一走“优先 Clipboard API，失败回退 textarea + execCommand”的降级路径，
 * 调用方不需要重复处理权限与兼容性。
 */
export async function copyText(value: string): Promise<boolean> {
  const text = String(value ?? '')
  if (!text) return false
  if (typeof navigator !== 'undefined' && navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 继续尝试兼容路径。
    }
  }
  try {
    const area = document.createElement('textarea')
    area.value = text
    area.setAttribute('readonly', '')
    area.style.position = 'fixed'
    area.style.top = '0'
    area.style.left = '-9999px'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    area.setSelectionRange(0, text.length)
    const ok = document.execCommand('copy')
    document.body.removeChild(area)
    return ok
  } catch {
    return false
  }
}
