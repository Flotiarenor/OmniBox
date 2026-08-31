// shell/frontend/src/core/bridge.ts

// 在普通浏览器（无 PyWebView）中，将后端 API 映射为 HTTP 请求。
// 这样 OmniBox 的前端也可以通过 nginx/SSL 部署到局域网浏览器访问。
function createHttpApi(): PyWebViewAPI {
  const httpApi: Record<string, (...args: any[]) => Promise<any>> = {}

  return new Proxy(httpApi, {
    get(_target, prop: string | symbol) {
      if (typeof prop !== 'string' || prop === 'then') {
        return undefined
      }

      if (!httpApi[prop]) {
        httpApi[prop] = async (...args: any[]) => {
          const response = await fetch(`/api/${encodeURIComponent(prop)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ args }),
          })

          const data = await response.json().catch(() => ({}))
          if (!response.ok || data.error) {
            // 通知 Shell 显示 API 状态（401/403/404/5xx），同时抛错给调用方
            window.dispatchEvent(new CustomEvent('omnibox:api-error', {
              detail: { status: response.status, method: String(prop) },
            }))
            throw new Error(data.error || `API ${prop} 请求失败 (HTTP ${response.status})`)
          }
          return data.result
        }
      }

      return httpApi[prop]
    },
  }) as PyWebViewAPI
}

// 在没有 PyWebView 的环境下自动注入 HTTP API shim。
function ensureBrowserApi() {
  if (!window.pywebview) {
    window.pywebview = { api: createHttpApi() }
  }
}

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
        // 如果不存在 PyWebView（普通浏览器），立即启用 HTTP API fallback
        ensureBrowserApi()
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