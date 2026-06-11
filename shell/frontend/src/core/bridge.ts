let _api: PyWebViewAPI | null = null

export function useBridge() {
  async function init(): Promise<void> {
    if (_api) return
    await new Promise<void>((resolve, reject) => {
      const check = () => {
        if (window.pywebview?.api) {
          _api = window.pywebview.api
          resolve()
        } else {
          setTimeout(check, 50)
        }
      }
      check()
      setTimeout(() => reject(new Error('PyWebView API 超时')), 5000)
    })
  }

  async function call(method: string, ...args: any[]) {
    if (!_api) throw new Error('Bridge 未初始化')
    return await _api[method](...args)
  }

  return { init, call }
}