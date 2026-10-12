// 壳偏好的**纯数据层**：类型、默认值、归一化、补丁合并、玻璃取值收敛。
//
// 刻意不 import 任何东西（连 `./var-rules` 都不引，名字校验在调用方做一次）：
// 这样它可以被 Node 直接加载做契约用例（`node --experimental-strip-types` 只做类型
// 擦除，不做"无扩展名 import"的解析），也就能把"前端归一化规则与后端一致"这件事
// 变成可执行的门禁，而不是靠两处肉眼看。传输（去抖、beacon）住在 `shell-prefs.ts`。

export type Theme = 'light' | 'dark'
export type StartPage = 'last' | 'first' | 'settings' | 'plugin'
export type BackgroundFit = 'cover' | 'contain' | 'fill' | 'none' | 'free'

/** 显示方式的取值集合（落盘校验与归一的唯一来源；界面文案在 background.ts）。 */
export const BACKGROUND_FIT_VALUES: BackgroundFit[] =
  ['cover', 'contain', 'fill', 'none', 'free']

export const START_PAGE_VALUES: StartPage[] = ['last', 'first', 'settings', 'plugin']

export interface StartupPrefs {
  /** 启动时打开：上次离开的页面 / 第一个插件 / 设置页 / 指定插件 */
  page: StartPage
  /** page === 'plugin' 时生效：目标插件的路由 */
  route: string
  /** 桌面窗口启动后进入全屏（浏览器访问时后端会明确拒绝，不产生副作用） */
  fullscreen: boolean
}

export interface BackgroundPrefs {
  /** 图片库内图片的绝对路径（`<config_dir>/backgrounds/<文件名>`），空串表示未设置 */
  path: string
  /** 缓存破坏版本号：同一路径换了图也要让浏览器重新取 */
  version: number
  fit: BackgroundFit
  posX: number
  posY: number
  zoom: number
  flipX: boolean
  flipY: boolean
}

/** 各插件内边栏宽度的取值区间（与 `data-obx-splitter-min` / 拖动上限同一口径）。
 *
 * 那一支 token 本身是 `--sub-sidebar-width`（写在 `variables.css` 的 `:root` 上，
 * 五个插件的侧栏与分隔条都读它）；设置页只用到这里的区间。 */
export const SIDEBAR_WIDTH = { min: 180, max: 480, def: 240 }

export interface ShellPrefs {
  theme: Theme
  /** 外观是**开放式键集**：壳的颜色变量、分隔条布局变量、插件贡献的变量同在表里 */
  colors: Record<string, string>
  background: BackgroundPrefs
  startup: StartupPrefs
  /** 上一次**安全退出**时所在的页面（本次会话内的上报只在内存） */
  last_route: string
}

export const DEFAULT_PREFS: ShellPrefs = {
  theme: 'dark',
  colors: {},
  background: { path: '', version: 0, fit: 'cover', posX: 50, posY: 50, zoom: 100,
                flipX: false, flipY: false },
  startup: { page: 'last', route: '', fullscreen: false },
  last_route: '',
}

// 玻璃两个变量的取值范围（与 shell/backend/shell_prefs.py 的 _GLASS_VAR_RANGES 一致）。
// 后端只接受区间内的整数，越界值会被整条拒收并回退默认 —— 前端因此必须发合法取值，
// 否则用户拖动滑块的结果会静默消失。
export const GLASS_LIMITS = { blurMin: 0, blurMax: 40, alphaMin: 30, alphaMax: 100 }
const GLASS_RANGES: Record<string, [number, number]> = {
  '--glass-blur': [GLASS_LIMITS.blurMin, GLASS_LIMITS.blurMax],
  '--glass-alpha': [GLASS_LIMITS.alphaMin, GLASS_LIMITS.alphaMax],
}

/** 玻璃变量的取值收敛到合法整数（带单位）；其它变量原样返回。 */
export function clampGlassValue(key: string, value: string): string {
  const range = GLASS_RANGES[key]
  if (!range) return value
  const match = /^(-?\d+(?:\.\d+)?)(px|%)$/.exec(value.trim())
  if (!match) return value
  const number = Math.min(range[1], Math.max(range[0], Math.round(Number(match[1]))))
  return `${number}${match[2]}`
}

export function pickStringMap(value: unknown): Record<string, string> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const out: Record<string, string> = {}
  Object.entries(value as Record<string, unknown>).forEach(([key, item]) => {
    if (typeof item === 'string') out[key] = item
  })
  return out
}

function pickNumber(value: unknown, fallback: number): number {
  const number = typeof value === 'number' ? value : Number.parseFloat(String(value))
  return Number.isFinite(number) ? number : fallback
}

function pickBackground(value: unknown, base: BackgroundPrefs): BackgroundPrefs {
  const source = (value && typeof value === 'object' ? value : {}) as Partial<BackgroundPrefs>
  return {
    path: typeof source.path === 'string' ? source.path : base.path,
    version: pickNumber(source.version, base.version),
    fit: BACKGROUND_FIT_VALUES.includes(source.fit as BackgroundFit)
      ? source.fit as BackgroundFit : base.fit,
    posX: pickNumber(source.posX, base.posX),
    posY: pickNumber(source.posY, base.posY),
    zoom: pickNumber(source.zoom, base.zoom),
    flipX: source.flipX === true,
    flipY: source.flipY === true,
  }
}

function pickStartup(value: unknown, base: StartupPrefs): StartupPrefs {
  const source = (value && typeof value === 'object' ? value : {}) as Partial<StartupPrefs>
  return {
    page: START_PAGE_VALUES.includes(source.page as StartPage)
      ? source.page as StartPage : base.page,
    route: typeof source.route === 'string' ? source.route : base.route,
    fullscreen: source.fullscreen === true,
  }
}

/** 把任意来源（注入的 JSON、旧 localStorage、后端返回值）收敛成完整偏好。 */
export function normalizePrefs(value: unknown, base: ShellPrefs): ShellPrefs {
  const source = (value && typeof value === 'object' ? value : {}) as Partial<ShellPrefs>
  const theme = source.theme === 'light' || source.theme === 'dark' ? source.theme : base.theme
  return {
    theme,
    colors: pickStringMap(source.colors),
    background: pickBackground(source.background, base.background),
    startup: pickStartup(source.startup, base.startup),
    last_route: typeof source.last_route === 'string' ? source.last_route : base.last_route,
  }
}

/** 把两个待发送补丁合并成一个。
 *
 * `colors` 必须按键合并：同一个去抖窗口里改了圆角又改了导航宽度时，后者不能把前者
 * 顶掉（后端对 colors 是读-改-写，前端这一层漏合并就会丢一次改动）。
 */
export function mergePatch(target: Record<string, unknown>,
                           patch: Record<string, unknown>): Record<string, unknown> {
  const next = { ...target, ...patch }
  if (target.colors || patch.colors) {
    next.colors = { ...(target.colors as object || {}), ...(patch.colors as object || {}) }
  }
  return next
}
