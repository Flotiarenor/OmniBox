// shell/frontend/src/core/toast.ts
//
// 壳的 Vue 代码与插件 iframe 共用同一个实现：/shell/toast.js 在壳首页与插件引导
// 片段里都先于其它脚本加载，DOM 渲染、去重、无障碍属性只有那一份。这里只是给
// 不认全局变量的 TypeScript 一个类型入口，不再维护第二套节点拼装逻辑。

export type ToastType = 'info' | 'success' | 'error' | 'warning'

interface ShellToast {
  show(message: string, type?: string, duration?: number): HTMLElement | null
  success(message: string): HTMLElement | null
  error(message: string): HTMLElement | null
  warning(message: string): HTMLElement | null
  info(message: string): HTMLElement | null
}

function shellToast(): ShellToast | undefined {
  return (window as Window & { Toast?: ShellToast }).Toast
}

export function toast(message: string, type: ToastType = 'info', duration = 2600): HTMLElement | null {
  const api = shellToast()
  // 壳页面一定加载了 /shell/toast.js，取不到只可能是脚本没发出来。此时静默返回而不是
  // 抛错：提示是反馈，不该让调用方的业务逻辑跟着中断。
  if (!api || typeof api.show !== 'function') return null
  return api.show(message, type, duration) || null
}

export const toastInfo = (msg: string) => toast(msg, 'info')
export const toastSuccess = (msg: string) => toast(msg, 'success')
export const toastError = (msg: string) => toast(msg, 'error')
