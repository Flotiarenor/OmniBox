// shell/frontend/src/core/bridge.ts
let _api: PyWebViewAPI | null = null
let _initPromise: Promise<void> | null = null

export function useBridge() {
  async function init(): Promise<void> {
    if (_api) return
    if (!_initPromise) {
      _initPromise = new Promise<void>((resolve, reject) => {
        const check = () => {
          if (window.pywebview?.api) {
            _api = window.pywebview.api
            resolve()
          } else {
            setTimeout(check, 50)
          }
        }
        check()
        setTimeout(() => reject(new Error('PyWebView API 超时（5秒）')), 5000)
      })
    }
    await _initPromise
  }

  async function call(method: string, ...args: any[]) {
    // 自动初始化（如果尚未完成）
    if (!_api) await init()
    return await _api![method](...args)
  }

  return { init, call }
}