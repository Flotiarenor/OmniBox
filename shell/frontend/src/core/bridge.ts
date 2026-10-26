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

let _api: PyWebViewAPI | null = null
let _initPromise: Promise<void> | null = null

/** 这台机器上是不是**桌面窗口**（WebView2 宿主）。
 *
 * pywebview 的 winforms/WebView2 后端总是注入 `window.chrome.webview`，普通浏览器
 * 没有这个对象。用它区分"等一下 pywebview"与"这里永远不会有 pywebview"：
 * 只有桌面模式才值得等，浏览器模式必须立刻回退到 HTTP 通道。
 */
function isDesktopHost(): boolean {
  return typeof (window as any).chrome?.webview !== 'undefined'
}

/** 等待 pywebview 注入它的 api（超时返回 null）。
 *
 * 为什么必须等：pywebview 的 `window.pywebview` 是**页面加载之后**才注入的，而
 * 模块脚本可能与它在同一批任务里跑。原先这里"检查一次，没有就立刻装 HTTP 替身"，
 * 于是在桌面窗口里也会固化成 HTTP 通道 —— 壳的调用看似正常，但凡是"只有桌面窗口
 * 才做得到"的接口（全屏）都会走到给浏览器准备的拒绝分支。
 */
function waitForPyWebView(timeoutMs: number): Promise<PyWebViewAPI | null> {
  return new Promise(resolve => {
    let settled = false
    const finish = (api: PyWebViewAPI | null) => {
      if (settled) return
      settled = true
      window.removeEventListener('pywebviewready', onReady)
      resolve(api)
    }
    const onReady = () => {
      const api = window.pywebview?.api
      if (api) finish(api)
    }
    // 先挂监听再查现值：反过来的话，事件正好在两步之间到达就永远等不到了
    window.addEventListener('pywebviewready', onReady)
    onReady()
    setTimeout(() => finish(window.pywebview?.api || null), timeoutMs)
  })
}

export function useBridge() {
  async function init(): Promise<void> {
    if (_api) return
    if (!_initPromise) {
      _initPromise = (async () => {
        if (!window.pywebview?.api) {
          // 桌面窗口等注入；浏览器模式不等（这里永远不会有 pywebview）
          const api = await waitForPyWebView(isDesktopHost() ? 8000 : 0)
          if (api) _api = api
        }
        if (!_api && window.pywebview?.api) _api = window.pywebview.api
        if (!_api) {
          // 真的没有 PyWebView（普通浏览器 / 普通浏览器模式的反向代理部署）：
          // 装上 HTTP 替身，页面照常可用
          window.pywebview = { api: createHttpApi() }
          _api = window.pywebview.api
        }
      })()
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
