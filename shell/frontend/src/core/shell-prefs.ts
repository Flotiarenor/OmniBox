// 壳偏好：启动页 / 启动全屏 / 上次页面 / 主题 / 自定义外观变量 / 背景 / 玻璃。
//
// 存储**不在** localStorage，而在后端 `<config_dir>/shell.json` 的 `prefs` 段：
// 桌面窗口的 localStorage 住在 WebView2 profile 的 LevelDB 里，日志被强杀截断之后
// Chromium 会在坏记录处停止回放，其后的写入在下次启动全部读不到 —— 表现为"设置存得进、
// 重启就忘"，且没有任何报错。
//
// 读：`serve_shell` 在 index.html 里注入 `window.__CUBBY_SHELL_STATE__`，
//     本模块在**首帧之前**同步读它，因此主题与配色不会闪一下默认值。
// 写：`system_set_shell_prefs(patch)` 局部更新，去抖合并（滑块每次 `input` 都写一次
//     会让后端每次 fsync + os.replace）。
//
// 纯数据部分（类型、归一化、补丁合并、玻璃取整）住在 `prefs-model.ts`：那里不 import
// 任何模块，因此可以被 Node 直接加载做契约用例。
import { useBridge } from './bridge'
import { isStorableVarValue, isValidVarName } from './var-rules'
import {
  clampGlassValue, DEFAULT_PREFS, GLASS_LIMITS, mergePatch, normalizePrefs,
  type BackgroundFit, type BackgroundPrefs, type ShellPrefs, type StartPage,
  type StartupPrefs, type Theme,
} from './prefs-model'

export {
  BACKGROUND_FIT_VALUES, clampGlassValue, DEFAULT_PREFS, GLASS_LIMITS, SIDEBAR_WIDTH,
  type BackgroundFit, type BackgroundPrefs, type ShellPrefs, type StartPage,
  type StartupPrefs, type Theme,
} from './prefs-model'

/** 后端返回的、在 index.html 里注入的首屏状态。 */
interface InjectedShellState {
  prefs?: Partial<ShellPrefs>
}

/** 去抖窗口：设置页的滑块每次 `input` 都要写，合并到窗口末尾一次。 */
const FLUSH_DELAY_MS = 300
/** 写失败后的重试延迟。 */
const RETRY_DELAY_MS = 2000

function readInjected(): InjectedShellState | null {
  const raw = (window as any).__CUBBY_SHELL_STATE__
  return raw && typeof raw === 'object' ? raw as InjectedShellState : null
}

// ===== 状态 =====

const injected = readInjected()

/** 当前生效的壳偏好（进程内唯一的可变副本，改动即生效）。 */
const state: ShellPrefs = normalizePrefs(injected?.prefs, DEFAULT_PREFS)

let dirty = false
let pendingPatch: Record<string, unknown> = {}
let timer: ReturnType<typeof setTimeout> | null = null
let retryTimer: ReturnType<typeof setTimeout> | null = null
let warned = false
let notify: (message: string) => void = message => console.warn('[ShellPrefs]', message)

/** 接入界面提示：写失败必须能看到，不能静默。 */
export function setShellPrefsNotifier(fn: (message: string) => void): void {
  notify = fn
}

export function shellPrefs(): ShellPrefs {
  return state
}

/** 注册"状态变化后重画 DOM"的钩子（appearance.ts / background.ts 各一份）。 */
const redrawHooks: (() => void)[] = []

export function onShellPrefsChanged(hook: () => void): void {
  redrawHooks.push(hook)
}

function redraw(): void {
  redrawHooks.forEach(hook => hook())
}

/** 供插件 iframe 通过 data-custom-colors 同步的那份 JSON。 */
export function colorsJson(): string {
  return JSON.stringify(state.colors)
}

// ===== 写：去抖合并 =====

function schedule(delay: number): void {
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => { void flushShellPrefs() }, delay)
}

/** 变更入口：改内存 → 重画 → 合并进待发送补丁。 */
export function patchShellPrefs(patch: Record<string, unknown>): void {
  pendingPatch = mergePatch(pendingPatch, patch)
  dirty = true
  redraw()
  schedule(FLUSH_DELAY_MS)
}

/** 立刻把待发送的偏好写回后端（切路由 / 松手 / 退出前调用）。 */
export async function flushShellPrefs(): Promise<void> {
  if (timer) {
    clearTimeout(timer)
    timer = null
  }
  if (!dirty || !Object.keys(pendingPatch).length) return
  const patch = pendingPatch
  pendingPatch = {}
  dirty = false
  await send(patch)
}

async function send(patch: Record<string, unknown>): Promise<void> {
  try {
    await useBridge().call('system_set_shell_prefs', patch)
    warned = false
  } catch (e: any) {
    const message = String(e?.message || e)
    if (message.includes('404')) {
      // 旧后端没有这个端点（构建产物比后端新 / 只升级了一半）：静默降级，
      // 界面上反复弹"保存失败"只会让人以为是数据坏了。
      console.warn('[ShellPrefs] 后端不支持 system_set_shell_prefs，本次改动未落盘')
      return
    }
    // 放回待发送队列：下一次变更、切路由或退出时会连同它一起重发
    pendingPatch = mergePatch(patch, pendingPatch)
    dirty = true
    if (message.includes('403') && !warned) {
      warned = true
      notify('当前使用者没有修改壳设置的权限（403），改动未保存')
      return
    }
    if (retryTimer) clearTimeout(retryTimer)
    retryTimer = setTimeout(() => { void flushShellPrefs() }, RETRY_DELAY_MS)
  }
}

/** 退出路径的最后一次写入：beacon 能在文档卸载后继续送达。 */
function flushOnExit(): void {
  if (!dirty || !Object.keys(pendingPatch).length) return
  const body = JSON.stringify({ args: [pendingPatch] })
  pendingPatch = {}
  dirty = false
  try {
    const blob = new Blob([body], { type: 'application/json' })
    if (navigator.sendBeacon?.('/api/system_set_shell_prefs', blob)) return
  } catch {
    // 落到下面的兜底
  }
  // beacon 不可用：发一次不等结果的请求，能不能送到看浏览器是否来得及发出去
  void fetch('/api/system_set_shell_prefs', {
    method: 'POST', keepalive: true,
    headers: { 'Content-Type': 'application/json' }, body,
  }).catch(() => { /* 退出路径上无处上报 */ })
}

if (typeof window !== 'undefined') {
  // 滑块松手即落盘：指针抬起是"这一档改完了"的自然分界，不必等去抖窗口
  window.addEventListener('pointerup', () => { void flushShellPrefs() }, true)
  window.addEventListener('beforeunload', flushOnExit)
}

// ===== 启动：与后端对齐 =====

/** 与后端对齐一次：拉回权威的偏好，并丢弃照旧状态算出来的待发送补丁。
 *
 * 为什么必须做：页面手里的偏好有可能会过期 —— 后端在我们加载页面之后重启过、
 * 或配置文件被换掉。此时若把照旧值算出来的补丁照发，就等于用旧值覆盖用户刚在新会话
 * 里改好的设置，而界面上看不出任何异常。
 *
 * 等待中的补丁（`dirty`）先发出去再对齐：那些改动是用户在本页面做的，语义上应当生效；
 * 而**其余**字段一律以后端为准 —— 这正是"页面停留在旧会话"时唯一的纠正点。
 */
async function reconcileWithServer(): Promise<void> {
  await flushShellPrefs()
  try {
    // 空补丁 = 不改任何东西，只把后端的当前值取回来
    const result = await useBridge().call('system_set_shell_prefs', {})
    if (result?.prefs) adoptServerPrefs(result.prefs)
  } catch (e: any) {
    // 404：后端不认识这个端点（构建产物比后端新），保持注入的取值；
    // 403：当前使用者不是管理员，壳偏好只读 —— 两种都不该打断启动。
    const message = String(e?.message || e)
    if (!message.includes('404') && !message.includes('403')) {
      console.warn('[ShellPrefs] 与后端对齐失败，沿用当前取值:', message)
    }
  }
}

/** 用后端返回的偏好整份替换本地状态，并丢弃待发送的补丁。
 *
 * 丢弃补丁是**必须的**：那些补丁是照着一份过期的状态算出来的，照发就等于用旧值覆盖
 * 用户刚在新会话里改好的设置，而界面上看不出任何异常。
 */
function adoptServerPrefs(prefs: unknown): void {
  Object.assign(state, normalizePrefs(prefs, state))
  pendingPatch = {}
  dirty = false
  if (timer) {
    clearTimeout(timer)
    timer = null
  }
  redraw()
}

/** 启动：与后端对齐，然后才解析首屏路由。 */
export async function loadShellPrefs(): Promise<void> {
  // 注入值是"页面被服务端渲染出来的那一刻"的快照。后端可能在那之后重启过，
  // 因此再做一次对齐；等待中的补丁（用户已经改过东西）时不覆盖本地状态。
  await reconcileWithServer()
  redraw()
}

// ===== 设置项写入入口 =====

export function setShellTheme(theme: Theme): void {
  state.theme = theme
  patchShellPrefs({ theme })
}

/** 写入单个外观变量（读-改-写：只发变动的那一个键）。
 *
 * 名字与取值都在这里再校验一次：后端会**整条拒收**畸形键并回退默认值，如果前端
 * 先把坏值写进了 `state`，界面显示的就会是一个永远不会生效的值。
 */
/** 单个插件的背景不透明度覆盖；缺失表示跟随壳的 `--glass-alpha`。 */
export const PLUGIN_ALPHA_PREFIX = '--plugin-glass-'

function pluginAlphaKey(plugin: string): string {
  return PLUGIN_ALPHA_PREFIX + plugin
}

export function pluginAlpha(plugin: string): number | null {
  const match = /^(-?\d+(?:\.\d+)?)%$/.exec(String(state.colors[pluginAlphaKey(plugin)] ?? '').trim())
  if (!match) return null
  const value = Math.round(Number(match[1]))
  if (!Number.isFinite(value)) return null
  return Math.min(GLASS_LIMITS.alphaMax, Math.max(GLASS_LIMITS.alphaMin, value))
}

export function setPluginAlpha(plugin: string, alpha: number | null): void {
  const key = pluginAlphaKey(plugin)
  if (alpha == null) {
    clearShellVar(key)
    return
  }
  const value = Math.min(GLASS_LIMITS.alphaMax, Math.max(GLASS_LIMITS.alphaMin, Math.round(alpha)))
  setShellVar(key, `${value}%`)
}


export function setShellVar(key: string, value: string): void {
  if (!isValidVarName(key) || !isStorableVarValue(value)) {
    console.warn('[ShellPrefs] 忽略非法的外观变量:', key, value)
    return
  }
  const effective = clampGlassValue(key, value)
  state.colors[key] = effective
  patchShellPrefs({ colors: { [key]: effective } })
}

/** 删除一个外观变量（恢复默认：留着旧值会永久压住 variables.css 的默认）。 */
export function clearShellVar(key: string): void {
  delete state.colors[key]
  patchShellPrefs({ colors: { [key]: null } })
}

export function setShellBackground(patch: Partial<BackgroundPrefs>): void {
  state.background = { ...state.background, ...patch }
  patchShellPrefs({ background: patch })
}

export function setShellStartup(patch: Partial<StartupPrefs>): void {
  state.startup = { ...state.startup, ...patch }
  patchShellPrefs({ startup: patch })
}

/** 上报当前所在页面：后端只放内存，安全退出（或空闲兜底）时才提交到 `last_route`。 */
export function rememberShellRoute(path: string): void {
  if (!path || path === '/' || path.startsWith('/status')) return
  if (state.last_route === path) return
  state.last_route = path
  patchShellPrefs({ last_route: path })
}

/** 首屏该去的路由：偏好指向的目标不存在时回退到第一个插件（没有插件则设置页）。 */
export function resolveStartRoute(pluginRoutes: string[]): string {
  const fallback = pluginRoutes[0] || '/settings'
  const { startup, last_route: last } = state
  if (startup.page === 'settings') return '/settings'
  if (startup.page === 'plugin') {
    return pluginRoutes.includes(startup.route) ? startup.route : fallback
  }
  if (startup.page === 'last') {
    // 上次那条路由对应的插件可能已被移除：只认当前仍然存在的路由
    if (last === '/settings' || pluginRoutes.includes(last)) return last
  }
  return fallback
}
