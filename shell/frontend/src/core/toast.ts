// shell/frontend/src/core/toast.ts
export type ToastType = 'info' | 'success' | 'error' | 'warning'

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
  el.textContent = message
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
