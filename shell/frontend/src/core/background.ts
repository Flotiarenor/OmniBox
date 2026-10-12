import {
  BACKGROUND_FIT_VALUES, GLASS_LIMITS, setShellBackground, setShellVar, shellPrefs,
  type BackgroundFit, type BackgroundPrefs,
} from './shell-prefs'

// 壳的自定义背景图与玻璃外观（集中设置页「背景」段）。
//
// 状态住在壳偏好里（`shell-prefs.ts` → `<config_dir>/shell.json`），本模块只负责
// "把状态画成 CSS"与"收敛越界取值"。为什么图片路径不走 omni-custom-colors 那套变量
// 广播，两个原因：
// 1. 那套通路的取值有 64 字符上限，而图片绝对路径轻易超过；
// 2. 插件页在透视模式下只需要**自己变透明**，不需要知道 URL —— iframe 内也无法
//    backdrop-filter 模糊父文档（独立 backdrop root），把 URL 广播进去没有收益。
// 真正需要插件知道的只有一个布尔量，走 html 上的 data-shell-glass 属性；它由"有没有
// 背景图"直接决定，不设独立开关（不透明面板会把整张背景图挡住）。
//
// 图片本身取自**图片库**（`<config_dir>/backgrounds`，见 shell_info）：用户选中的
// 图片由后端复制进去，`/file` 只额外放行这一个目录，因此这里的 path 一定是库内条目。

export type { BackgroundFit }

/** 显示方式：对应 object-fit 的四种取值，外加"自由"（object-fit: none + 手动位置与缩放）。 */
export type BackgroundState = BackgroundPrefs

export interface GlassState {
  /** 面板背景的模糊强度（px） */
  blur: number
  /** 面板背景的不透明度（%）：越低背景图越明显，可读性也越低 */
  alpha: number
}

/** 显示方式选项：label 是界面文案，help 说明它映射到的 CSS 取值。 */
export const BACKGROUND_FITS: { value: BackgroundFit; label: string; help: string }[] = [
  { value: 'cover', label: '填充', help: 'object-fit: cover —— 铺满窗口，超出部分裁掉，不改变比例' },
  { value: 'contain', label: '适应', help: 'object-fit: contain —— 完整显示整张图，窗口留出空边' },
  { value: 'fill', label: '拉伸', help: 'object-fit: fill —— 拉满窗口，比例可能失真' },
  { value: 'none', label: '居中', help: 'object-fit: none —— 按原图尺寸居中，超出部分裁掉' },
  { value: 'free', label: '自由', help: 'object-fit: none —— 按原图尺寸摆放，位置由下面的滑块决定' },
]

/** 未设置过的显示方式参数（恢复默认显示方式时也回到这一组）。 */
export const BACKGROUND_DEFAULTS = {
  fit: 'cover' as BackgroundFit, posX: 50, posY: 50, zoom: 100, flipX: false, flipY: false,
}
export const BACKGROUND_LIMITS = { zoomMin: 10, zoomMax: 400, posMin: 0, posMax: 100 }

/** 玻璃的取值与取值上限都住在壳偏好里（后端按同一组区间校验）。 */
export const GLASS_DEFAULTS: GlassState = { blur: 18, alpha: 84 }
export { GLASS_LIMITS }

/** 背景图 URL。
 *
 * 只用到 path 与 version，因此缩略图网格传 `{ path, version: 0 }` 也能复用：
 * 网格里的图不跟着版本号刷新，取的是当前文件。
 *
 * 走壳级 `/file`（**不带** `plugin` 参数）：后端在无插件上下文时把 data_root 与
 * 图片库一并作为允许根（file_server.serve_media_file），所以这里用的是与插件完全
 * 相同的那条文件服务，没有新的发布路由。
 */
export function backgroundUrl(state: { path: string; version?: number }): string {
  if (!state.path) return ''
  const version = state.version ? `&v=${state.version}` : ''
  return `/file?path=${encodeURIComponent(state.path)}${version}`
}

/** 两个图片路径是否指同一个文件：只归一化分隔符与尾部分隔符，**不做大小写折叠**。
 *
 * 大小写折叠会把"不同"判成"相同"，于是跳过"这张图还没进图片库"的判定，后果是旧版
 * 存下的库外路径被当成库内条目、背景不显示；反过来把"相同"判成"不同"只是多导入一份。
 */
export function samePath(a: string, b: string): boolean {
  const norm = (value: string) =>
    String(value || '').trim().replace(/[\\/]+/g, '/').replace(/\/+$/, '')
  return norm(a) === norm(b)
}

// ===== 显示方式 → CSS =====
// 背景画在 .app-shell::before 上（见 styles/shell.css）：object-fit 落在 background-size，
// object-position 落在 background-position，镜像落在 transform。
//
// 这里**不用** CSS 的 cover/contain 关键字配 `transform: scale()` 做缩放：两者组合后
// 缩放的是"已经按窗口裁好的那一块"，放大时看不到被裁掉的部分、缩小时图片周围露出应用
// 底色 —— 那是把裁切结果整体缩小，不是缩放图片本身。缩放因此参与 background-size 的
// 计算：先按原图尺寸算出 object-fit 后的尺寸，再乘缩放系数。这要求知道原图的像素尺寸，
// 也就是下面的 naturalSizes 缓存。

interface Size { width: number; height: number }

/** 原图尺寸：`路径|版本` → 像素尺寸。没读到之前先用关键字画第一帧。 */
const naturalSizes = new Map<string, Size>()
const pendingSizes = new Set<string>()
let resizeBound = false

function imageKey(state: BackgroundState): string {
  return `${state.path}|${state.version}`
}

function viewportSize(): Size {
  // 背景层与窗口等大（.app-shell 是 100vw × 100vh），窗口尺寸就是容器尺寸
  return { width: window.innerWidth || 1, height: window.innerHeight || 1 }
}

/** 图片的渲染尺寸：object-fit 的四种取值，再乘缩放系数。 */
function renderedSize(state: BackgroundState, natural: Size): Size {
  const zoom = state.zoom / 100
  const view = viewportSize()
  if (state.fit === 'fill') return { width: view.width * zoom, height: view.height * zoom }
  let scale = zoom
  if (state.fit === 'cover') {
    scale *= Math.max(view.width / natural.width, view.height / natural.height)
  } else if (state.fit === 'contain') {
    scale *= Math.min(view.width / natural.width, view.height / natural.height)
  }
  // none / free：按原图尺寸，只乘缩放
  return { width: natural.width * scale, height: natural.height * scale }
}

/** 原图尺寸还没读到时的兜底：与 zoom=100 算出来的一致，只是暂时不能缩放。 */
function keywordSize(fit: BackgroundFit): string {
  if (fit === 'fill') return '100% 100%'
  if (fit === 'contain') return 'contain'
  if (fit === 'none' || fit === 'free') return 'auto'
  return 'cover'
}

function pixelSize(size: Size): string {
  return `${Math.round(size.width)}px ${Math.round(size.height)}px`
}

/** 读原图尺寸，读完按像素尺寸重算一次（探针图走浏览器缓存，不额外拉一遍字节）。 */
function requestNaturalSize(state: BackgroundState, url: string): void {
  const key = imageKey(state)
  if (pendingSizes.has(key)) return
  pendingSizes.add(key)
  const probe = new Image()
  probe.onload = () => {
    pendingSizes.delete(key)
    if (probe.naturalWidth > 0 && probe.naturalHeight > 0) {
      naturalSizes.set(key, { width: probe.naturalWidth, height: probe.naturalHeight })
      applyBackground()
    }
  }
  probe.onerror = () => { pendingSizes.delete(key) }
  probe.src = url
}

/** 窗口尺寸变化要重算像素尺寸：cover / contain 的取值与窗口宽高比有关。 */
function bindResize(): void {
  if (resizeBound) return
  resizeBound = true
  window.addEventListener('resize', () => applyBackground())
}

/** object-position：只有自由模式可调。
 *
 * 翻转镜像的是整个渲染结果（绕容器中心），因此位置要取镜像点 `100 - X`：
 * 不补偿时，位置不在 50% 的图片一翻转就会跳到图片的另一处。
 */
function backgroundPosition(state: BackgroundState): string {
  if (state.fit !== 'free') return 'center'
  const x = state.flipX ? 100 - state.posX : state.posX
  const y = state.flipY ? 100 - state.posY : state.posY
  return `${x}% ${y}%`
}

/** 只承载镜像：缩放已经在 background-size 里算掉了。 */
function backgroundTransform(state: BackgroundState): string {
  const parts: string[] = []
  if (state.flipX) parts.push('scaleX(-1)')
  if (state.flipY) parts.push('scaleY(-1)')
  return parts.length ? parts.join(' ') : 'none'
}

function clamp(value: unknown, min: number, max: number, fallback: number): number {
  const n = typeof value === 'number' ? value : Number.parseFloat(String(value))
  if (!Number.isFinite(n)) return fallback
  return Math.min(max, Math.max(min, Math.round(n)))
}

/** 收敛取值：旧版本残留、手改配置文件都不会带出越界的显示参数。 */
function normalizeBackground(state: Partial<BackgroundState>): BackgroundState {
  const fits = BACKGROUND_FIT_VALUES
  return {
    path: typeof state.path === 'string' ? state.path : '',
    version: typeof state.version === 'number' && Number.isFinite(state.version) ? state.version : 0,
    fit: fits.includes(state.fit as BackgroundFit) ? state.fit as BackgroundFit : BACKGROUND_DEFAULTS.fit,
    posX: clamp(state.posX, BACKGROUND_LIMITS.posMin, BACKGROUND_LIMITS.posMax, BACKGROUND_DEFAULTS.posX),
    posY: clamp(state.posY, BACKGROUND_LIMITS.posMin, BACKGROUND_LIMITS.posMax, BACKGROUND_DEFAULTS.posY),
    zoom: clamp(state.zoom, BACKGROUND_LIMITS.zoomMin, BACKGROUND_LIMITS.zoomMax, BACKGROUND_DEFAULTS.zoom),
    flipX: state.flipX === true,
    flipY: state.flipY === true,
  }
}

export function getBackground(): BackgroundState {
  return normalizeBackground(shellPrefs().background)
}

/** 玻璃参数的两个 CSS 变量名。
 *
 * 它们住在**统一外观变量表**里（`colors`），必须经 `setShellVar` 走
 * data-custom-colors 广播，不能只写壳自己的 html 内联：插件 iframe 是**独立文档**，
 * 壳的内联它读不到，base.css 里的 `var(--glass-alpha)` 会退回 variables.css 的默认值
 * —— 表现就是"调了强度与不透明度，只有设置页在变"。
 */
export const GLASS_BLUR_VAR = '--glass-blur'
export const GLASS_ALPHA_VAR = '--glass-alpha'

/** 从变量文本里读出玻璃参数；缺失或畸形时回默认值。 */
function readGlassVar(key: string, fallback: number): number {
  const raw = shellPrefs().colors[key]
  const match = /^(-?\d+(?:\.\d+)?)(px|%)$/.exec(String(raw ?? '').trim())
  if (!match) return fallback
  const number = Number(match[1])
  return Number.isFinite(number) ? Math.round(number) : fallback
}

export function getGlass(): GlassState {
  return {
    blur: clamp(readGlassVar(GLASS_BLUR_VAR, GLASS_DEFAULTS.blur),
                GLASS_LIMITS.blurMin, GLASS_LIMITS.blurMax, GLASS_DEFAULTS.blur),
    alpha: clamp(readGlassVar(GLASS_ALPHA_VAR, GLASS_DEFAULTS.alpha),
                 GLASS_LIMITS.alphaMin, GLASS_LIMITS.alphaMax, GLASS_DEFAULTS.alpha),
  }
}

/** 把当前状态写到 html 上：CSS 变量负责外观，属性负责告诉插件页。
 *
 * `data-shell-glass` 是插件页唯一需要的东西（base.css 据此让 body / #app 让出底色）。
 * 它必须写在 html 上并由注入脚本的 MutationObserver 同步，插件侧零代码。
 *
 * 它由"有没有背景图"直接决定，不另设开关：不透明面板会把整张背景图挡住，背景图
 * 与半透明面板是同一件事的两面，分开开关只会得到"选了图但看不见"这一种状态。
 */
export function applyBackground(state?: BackgroundState, glass?: GlassState): void {
  const bg = state ?? getBackground()
  const gl = glass ?? getGlass()
  const el = document.documentElement
  const url = backgroundUrl(bg)

  if (url) {
    const natural = naturalSizes.get(imageKey(bg))
    el.style.setProperty('--shell-bg-image', `url("${url}")`)
    // 缩放参与尺寸计算（见上面 renderedSize 的说明），因此这里给的是像素尺寸；
    // 原图尺寸没读到之前退回关键字，与 zoom=100 的取值一致。
    el.style.setProperty('--shell-bg-size',
      natural ? pixelSize(renderedSize(bg, natural)) : keywordSize(bg.fit))
    el.style.setProperty('--shell-bg-position', backgroundPosition(bg))
    el.style.setProperty('--shell-bg-transform', backgroundTransform(bg))
    bindResize()
    if (!natural) requestNaturalSize(bg, url)
  } else {
    el.style.removeProperty('--shell-bg-image')
    el.style.removeProperty('--shell-bg-size')
    el.style.removeProperty('--shell-bg-position')
    el.style.removeProperty('--shell-bg-transform')
  }

  el.style.setProperty(GLASS_BLUR_VAR, `${gl.blur}px`)
  el.style.setProperty(GLASS_ALPHA_VAR, `${gl.alpha}%`)
  el.setAttribute('data-shell-glass', url ? 'on' : 'off')
}

/** 改背景状态：换图时递增版本号，否则浏览器会用缓存里的旧图。 */
export function setBackground(patch: Partial<BackgroundState>): void {
  const prev = getBackground()
  const next = normalizeBackground({ ...prev, ...patch })
  if (next.path !== prev.path) next.version = Date.now()
  setShellBackground(next)
  applyBackground(next)
}

export function setGlass(patch: Partial<GlassState>): void {
  const next = { ...getGlass(), ...patch }
  setShellVar(GLASS_BLUR_VAR, `${next.blur}px`)
  setShellVar(GLASS_ALPHA_VAR, `${next.alpha}%`)
  applyBackground(undefined, next)
}
