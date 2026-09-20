// Shell 启动 / 窗口偏好（集中设置页「启动与窗口」段的持久化）。
//
// 与主题一样存 localStorage：这些值由**前端**在挂载前读取（决定首屏路由），
// 后端不需要知道它们，也就不必为它们增加一套配置读写端点。
export type StartPage = 'last' | 'first' | 'settings' | 'plugin'

export interface StartupPrefs {
  /** 启动时打开：上次离开的页面 / 第一个插件 / 设置页 / 指定插件 */
  page: StartPage
  /** page === 'plugin' 时生效：目标插件的路由 */
  route: string
  /** 桌面窗口启动后进入全屏（浏览器访问时后端是空实现，不会有副作用） */
  fullscreen: boolean
}

const STARTUP_KEY = 'omni-startup'
const LAST_ROUTE_KEY = 'omni-last-route'
const PAGES: StartPage[] = ['last', 'first', 'settings', 'plugin']

export function getStartupPrefs(): StartupPrefs {
  try {
    const raw = JSON.parse(localStorage.getItem(STARTUP_KEY) || '{}')
    return {
      page: PAGES.includes(raw?.page) ? raw.page : 'last',
      route: typeof raw?.route === 'string' ? raw.route : '',
      fullscreen: raw?.fullscreen === true,
    }
  } catch {
    // 存储内容损坏时按默认值处理，不影响启动
    return { page: 'last', route: '', fullscreen: false }
  }
}

export function setStartupPrefs(prefs: StartupPrefs): void {
  localStorage.setItem(STARTUP_KEY, JSON.stringify(prefs))
}

/** 记下用户当前所在页面，供「上次离开的页面」还原（/status 与根路径不记）。 */
export function rememberLastRoute(path: string): void {
  if (!path || path === '/' || path.startsWith('/status')) return
  localStorage.setItem(LAST_ROUTE_KEY, path)
}

/** 首屏该去的路由：偏好指向的目标不存在时回退到第一个插件（没有插件则设置页）。 */
export function resolveStartRoute(pluginRoutes: string[]): string {
  const prefs = getStartupPrefs()
  const fallback = pluginRoutes[0] || '/settings'
  if (prefs.page === 'settings') return '/settings'
  if (prefs.page === 'plugin') {
    return pluginRoutes.includes(prefs.route) ? prefs.route : fallback
  }
  if (prefs.page === 'last') {
    const last = localStorage.getItem(LAST_ROUTE_KEY) || ''
    // 上次那条路由对应的插件可能已被移除：只认当前仍然存在的路由
    if (last === '/settings' || pluginRoutes.includes(last)) return last
  }
  return fallback
}
