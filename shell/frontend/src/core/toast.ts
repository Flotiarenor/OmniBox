// shell/frontend/src/core/toast.ts
import { ensureIcons, iconHtml } from './icons.generated'

export type ToastType = 'info' | 'success' | 'error' | 'warning'

/**
 * 每种 Toast 的前缀图标。用壳的图标集而不是 CSS `content` 里的符号：
 * 伪元素只能放文本，放不了 SVG（且符号字形由系统字体决定，跨平台不一致）。
 */
const TOAST_ICONS: Record<ToastType, string> = {
  success: 'icon:circle-check',
  error: 'icon:circle-x',
  warning: 'icon:triangle-alert',
  info: 'icon:info',
}

let _container: HTMLElement | null = null

function ensureContainer(): HTMLElement {
  if (!_container) {
    _container = document.createElement('div')
    _container.className = 'toast-container'
    document.body.appendChild(_container)
  }
  return _container
}

export function toast(message: string, type: ToastType = 'info', duration = 2600) {
  const el = document.createElement('div')
  el.className = `toast toast-${type}`
  // 图标单独插入，消息始终走文本节点：消息内容不经过 innerHTML。
  ensureIcons()
  const icon = document.createElement('span')
  icon.className = 'toast-icon'
  icon.innerHTML = iconHtml(TOAST_ICONS[type] || TOAST_ICONS.info)
  el.append(icon, document.createTextNode(message))
  ensureContainer().appendChild(el)
  requestAnimationFrame(() => el.classList.add('show'))
  setTimeout(() => {
    el.classList.remove('show')
    setTimeout(() => el.remove(), 250)
  }, duration)
}

export const toastInfo = (msg: string) => toast(msg, 'info')
export const toastSuccess = (msg: string) => toast(msg, 'success')
export const toastError = (msg: string) => toast(msg, 'error')
