<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor -->
<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { loadPlugins, getPlugins, type PluginChromeState } from './core/plugin-loader'
import {
  disposeFrames,
  ensureFrame,
  LIFECYCLE_MESSAGES,
  noteFrameEpoch,
  refreshFrame,
  type LifecycleMessage,
} from './core/plugin-visibility'
import SettingsView from './views/SettingsView.vue'
import StatusView from './views/StatusView.vue'
import Icon from './components/Icon.vue'
import PluginChrome from './components/PluginChrome.vue'
import PluginSettingsModal from './components/PluginSettingsModal.vue'
import NativePanel from './components/NativePanel.vue'
import type { SchemaAction } from './core/plugin-content'
import { ensureIcons, iconHtml } from './core/icons.generated'
import { toast, toastError, toastInfo, toastSuccess } from './core/toast'
import { useBridge } from './core/bridge'
import { escapeHtml as escapeHtmlText } from '@vue/shared'
import { rememberLastRoute, resolveStartRoute } from './core/preferences'
import { isPluginPageUrl } from './core/plugin-url'
import { isValidVarName, isValidVarValue, setAppearanceVar } from './core/appearance'
import { flushShellPrefs, onShellPrefsChanged, pluginAlpha, setPluginAlpha, setShellPrefsNotifier, shellPrefs } from './core/shell-prefs'
import { getGlass } from './core/background'

const router = useRouter()
const route = useRoute()
const bridge = useBridge()
const isReady = ref(false)
const error = ref('')
const navHidden = ref(false)

const visitedPlugins = reactive<Record<string, boolean>>({})
const activePlugin = ref<string | null>(null)
// 声明了 frontend.chrome 的插件：壳渲染页面骨架，iframe 只装内容。
// 视图 id、标题/副标题/徽标/状态栏都由插件通过消息上报，壳不解释业务。
const chromeViews = reactive<Record<string, string>>({})
const chromeStates = reactive<Record<string, PluginChromeState>>({})
// 附属插件 native 面板：逻辑在隐藏 iframe 里，渲染在壳里；一次只开一个。
const nativePanel = ref<{ host: string; plugin: string; url: string; title: string; toolbar: SchemaAction[] } | null>(null)
const nativeRefs = new Map<string, { action: (id: string, payload?: unknown) => void }>()

function setNativeRef(name: string, el: unknown): void {
  if (el) nativeRefs.set(name, el as { action: (id: string, payload?: unknown) => void })
  else nativeRefs.delete(name)
}

function onNativeToolbar(name: string, actions: SchemaAction[]): void {
  if (nativePanel.value?.host !== name) return
  nativePanel.value = { ...nativePanel.value, toolbar: actions }
}

function onNativeToolbarAction(name: string, action: { id: string; payload?: unknown }): void {
  const panel = nativePanel.value
  // native 顶栏的「设置」是页面级动作：不能回发给隐藏逻辑页，否则它会在不可见的
  // iframe 里打开一套自绘设置。由壳打开 PluginSettingsModal，保存后再回 settings-saved。
  if (panel && panel.host === name && action.id === 'open-settings') {
    openNativeSettings(panel)
    return
  }
  nativeRefs.get(name)?.action(action.id, action.payload)
}
// 插件设置弹窗：schema / 值 / 保存全归插件后端，壳只负责盖住整页渲染。
// 只有「设置能由 settings_schema 完整表达」的插件才在壳里打开；image-viewer 的设置
// 带按目录作用域与根目录列表控件，仍由内容页自己渲染（见 docs/plugin-page-architecture.md）。
const SHELL_SETTINGS_PLUGINS = new Set([
  'group-mesh',
  'document-reader',
  'manga-library',
  'media-player',
])
const settingsPanel = ref<{
  host: string
  plugin: string
  title: string
  target: 'content' | 'native'
  showAppearance?: boolean
} | null>(null)
// 插件 iframe 加载到错误标记页（后端返回 data-status-page 属性）时记录状态码
const frameErrors = reactive<Record<string, number>>({})
// 重试计数：改变 iframe src 的查询参数强制重新加载
const reloadCounters = reactive<Record<string, number>>({})

// 必须定义在下面的 route watcher 之前：watcher 是 immediate，会在 setup 期同步执行
const plugins = computed(() => getPlugins())
const keepAlivePlugins = computed(() => plugins.value.filter(p => p.keepAlive))
// 默认组：不声明 keepAlive 的插件离开即卸载 iframe，回来是干净重载
const transientPlugins = computed(() => plugins.value.filter(p => !p.keepAlive))
const currentPlugin = computed(() => route.meta.pluginName as string || '')
const isSettings = computed(() => route.path === '/settings')
const isStatus = computed(() => route.path === '/status')

function onFrameLoad(e: Event, name: string) {
  try {
    const doc = (e.target as HTMLIFrameElement).contentDocument
    const code = doc?.documentElement.getAttribute('data-status-page')
    if (code) {
      frameErrors[name] = Number(code) || 0
      // 跳转到壳内建状态视图展示错误（保留来源路径用于「重试」返回）
      router.push({
        path: '/status',
        query: { code: String(frameErrors[name]), from: route.fullPath },
      })
    } else {
      delete frameErrors[name]
    }
  } catch {
    // 跨域无法读取内容时不处理，保持原行为
    delete frameErrors[name]
  }
  // 新挂载/重载的 frame 必须显式登记初始状态：不保活的插件在 iframe 加载完成前
  // 就可能被切走，此时它还没有过任何状态；不登记的话它会在后台被误判为"可见"，
  // 第一次真正显示时反而收不到通知。重载（重试 / 设置变更改 src）会递增文档代次，
  // 让状态机丢掉旧文档的结论并重新通知一次 —— 否则后台重载的新文档会以为自己可见。
  const mounted = activePlugin.value === name
  const epoch = (frameEpochs.get(name) || 0) + 1
  frameEpochs.set(name, epoch)
  frameMounted.set(name, mounted)
  const reloaded = noteFrameEpoch(name, epoch)
  const messages = reloaded
    ? (mounted && windowVisible.value ? [LIFECYCLE_MESSAGES.shown] : HIDDEN_BY_DEFAULT)
    : ensureFrame(name, mounted)
  notifyFrame(name, messages)
  refreshFrameVisibility(name)
  postAppearance(name)
  // 壳渲染 chrome 的插件：内容页就绪后会发 omnibox:ready，这里是首帧兜底；
  // 重复发送视图 id 是幂等的，内容页按当前面板重新落一次即可。
  if (chromeViews[name]) {
    window.setTimeout(() => postChromeView(name), 300)
  }
}

function pluginSrc(p: { name: string; entryUrl: string; chrome?: unknown }): string {
  // 有 chrome 声明的插件只加载内容页；骨架由壳渲染。
  const base = p.chrome
    ? p.entryUrl + (p.entryUrl.includes('?') ? '&' : '?') + 'content=1'
    : p.entryUrl
  const n = reloadCounters[p.name] || 0
  if (!n) return base
  return base + (base.includes('?') ? '&' : '?') + '_r=' + n
}

/** 单插件背景不透明度：有覆盖用覆盖，否则跟随壳的玻璃不透明度。 */
const prefsTick = ref(0)
function pluginAlphaFor(name: string): number {
  prefsTick.value  // 建立依赖：偏好变化时重算
  return pluginAlpha(name) ?? getGlass().alpha
}

function postAppearance(name: string): void {
  postToFrame(name, {
    type: 'omnibox:appearance',
    alpha: pluginAlphaFor(name),
    follow: pluginAlpha(name) === null,
    shellAlpha: getGlass().alpha,
  })
}

// ===== 插件生命周期通知 =====
// 声明了 `keepAlive: true` 的插件用 keep-alive（v-show 隐藏），切走以后其定时器 /
// rAF 自循环 / 轮询仍在跑，插件前端也无从知道自己的 iframe 是否可见。宿主因此在三种
// 情形下通知插件（不保活的插件切走即卸载，只会收到 dispose）：
//   1. 切换插件（activePlugin 变化）：刚变为非活动 → hidden，刚变为活动 → shown；
//   2. 窗口 visibilitychange（最小化 / 切标签页）：与"可见"取交集；
//   3. beforeunload / pagehide：已挂载的 frame 一律 dispose。
// "该不该发"由 plugin-visibility 状态机判定（只发真正的状态变化）。
const frameRefs = new Map<string, HTMLIFrameElement>()
// 每个 frame 是否已作为活动插件挂载（不保活的插件切走即卸载）
const frameMounted = new Map<string, boolean>()
// 每个 frame 的文档代次（每次 iframe load 递增）：重载后旧文档的可见性结论作废
const frameEpochs = new Map<string, number>()
// 窗口级可见性：最小化 / 切标签页时为 false
const windowVisible = ref(true)
const HIDDEN_BY_DEFAULT: LifecycleMessage[] = []

function setFrameRef(name: string, el: Element | null) {
  const frame = el?.querySelector('iframe')
  if (frame) frameRefs.set(name, frame as HTMLIFrameElement)
}

function sendToFrame(name: string, message: string) {
  const frame = frameRefs.get(name)
  const target = frame?.contentWindow
  if (!target) return
  try {
    target.postMessage({ type: message }, window.location.origin)
  } catch (e) {
    console.warn(`[Shell] 向插件 ${name} 发送 ${message} 失败:`, e)
  }
}

// 以状态机给出的通知为准发消息（状态机内部已去重）
function notifyFrame(name: string, messages: LifecycleMessage[]) {
  if (!frameRefs.has(name)) return
  messages.forEach(message => sendToFrame(name, message))
}

function refreshFrameVisibility(name: string) {
  const state = {
    mounted: !!frameMounted.get(name),
    active: activePlugin.value === name,
    windowVisible: windowVisible.value,
  }
  notifyFrame(name, refreshFrame(name, state))
}

// 路由或窗口可见性变化后，让所有已挂载 frame 的可见性状态重新对齐
function refreshAllFrameVisibility() {
  frameRefs.forEach((_frame, name) => refreshFrameVisibility(name))
}

// 不保活组：离开即卸载 iframe，卸载前发一次 dispose
function disposeLeavingFrames(keepAliveNames: Set<string>) {
  const leaving = Array.from(frameRefs.keys()).filter(name => {
    if (keepAliveNames.has(name)) return false
    if (!frameMounted.get(name)) return false
    frameMounted.set(name, false)
    return true
  })
  disposeFrames(leaving).forEach((message, index) => sendToFrame(leaving[index], message))
}

function onVisibilityChange() {
  windowVisible.value = document.visibilityState !== 'hidden'
  refreshAllFrameVisibility()
}

function onPageHide() {
  // 页面即将卸载：一次性通知所有 frame 释放资源（监听器 / 定时器 / rAF）
  const names = Array.from(frameRefs.keys())
  disposeFrames(names).forEach((message, index) => sendToFrame(names[index], message))
}

function handleRetry(from: string) {
  // 从错误视图「重试」：强制重载对应插件再返回
  const p = getPlugins().find(pl => pl.route === from)
  if (p) {
    delete frameErrors[p.name]
    reloadCounters[p.name] = (reloadCounters[p.name] || 0) + 1
  }
  router.push(from || '/')
}

function onApiError(ev: Event) {
  const d = (ev as CustomEvent).detail
  if (!d || typeof d.status !== 'number') return
  const method = d.method || ''
  if (d.status === 401) toastError('访问令牌无效或缺失（401），请刷新页面重试')
  else if (d.status === 403) toastError('无权访问该资源（403）')
  else if (d.status === 404) toastError(`接口不存在（404）: ${method}`)
  else if (d.status >= 500) toastError(`后端错误（${d.status}）: ${method}`)
}

// ===== 分隔条：插件 iframe 拖出来的用户值回到壳持久化 =====
// 插件 iframe 里的拖动只改它自己文档的渲染值，用户值必须经这里写进壳偏好，
// 否则切走再回来就丢了。两个入口（壳页面直接拖 / 插件 iframe 发消息）共用同一份校验。
function commitAppearanceVar(key: unknown, value: unknown): void {
  if (!isValidVarName(key) || !isValidVarValue(value)) {
    console.warn('[Shell] 忽略非法的外观变量写入:', key, value)
    return
  }
  setAppearanceVar(key, value)
}

function isRegisteredFrameSource(source: unknown): boolean {
  if (!source) return false
  for (const frame of frameRefs.values()) {
    if (frame.contentWindow === source) return true
  }
  return false
}

function frameNameForSource(source: unknown): string | null {
  if (!source) return null
  for (const [name, frame] of frameRefs) {
    if (frame.contentWindow === source) return name
  }
  return null
}

function postToFrame(name: string, payload: Record<string, unknown>): void {
  const frame = frameRefs.get(name)
  const target = frame?.contentWindow
  if (!target) return
  try {
    target.postMessage(payload, window.location.origin)
  } catch (e) {
    console.warn(`[Shell] 向插件 ${name} 发送消息失败:`, e)
  }
}

function postChromeView(name: string): void {
  const view = chromeViews[name]
  if (!view) return
  postToFrame(name, { type: 'omnibox:view', view })
}

function onChromeView(name: string, view: string): void {
  if (nativePanel.value?.host === name) {
    nativePanel.value = null
    if (settingsPanel.value?.target === 'native' && settingsPanel.value.host === name) {
      settingsPanel.value = null
    }
  }
  if (!view) return
  // 不去重：内容页可能刚把高亮清成 ""，此时再点回上一个导航项必须重新发送；
  // 重复发送是幂等的，去重反而会吞掉"点回原分类"。
  chromeViews[name] = view
  // 壳点击导航表示回到一级视图：清掉内容页之前设置的阅读态/二级页高亮。
  if (chromeStates[name]) delete chromeStates[name].view
  postToFrame(name, { type: 'omnibox:view', view })
}

function onChromeAction(name: string, action: string): void {
  if (!action) return
  // 设置是页面级弹窗：内容 iframe 内的 `.modal` 盖不住壳侧栏，由壳按插件 schema 渲染。
  // 保存成功后回一条 `settings-saved`，内容页据此重读设置并刷新当前视图。
  if (action === 'settings' && SHELL_SETTINGS_PLUGINS.has(name)) {
    openPluginSettings(name)
    return
  }
  postToFrame(name, { type: 'omnibox:action', action })
}

/** 设置弹窗保存成功：关闭后让来源页重读设置（各插件按自己的状态模型收尾）。 */
function onSettingsSaved(): void {
  const panel = settingsPanel.value
  settingsPanel.value = null
  if (!panel) return
  if (panel.target === 'native') {
    // native companion 的可见内容在壳里，设置语义在隐藏逻辑页：回 settings-saved
    // 让逻辑页重读设置并重发 blocks。
    nativeRefs.get(panel.host)?.action('settings-saved')
    return
  }
  postToFrame(panel.host, { type: 'omnibox:action', action: 'settings-saved' })
}

function onChromeQuery(name: string, query: string): void {
  postToFrame(name, { type: 'omnibox:query', query })
}

/** 壳侧扩展入口（PluginExtensions）打开 native 面板。 */
function onChromeNative(name: string, payload: { plugin: string; url: string; title: string }): void {
  const plugin = String(payload.plugin || '')
  const url = String(payload.url || '')
  if (!isPluginPageUrl(plugin, url)) return
  if (settingsPanel.value?.target === 'native' && settingsPanel.value.host === name) {
    settingsPanel.value = null
  }
  if (nativePanel.value) {
    const current = nativePanel.value
    if (current.host === name && current.plugin === plugin && current.url === url) {
      nativePanel.value = null
      return
    }
  }
  nativePanel.value = { host: name, plugin, url, title: String(payload.title || plugin), toolbar: [] }
}

function openPluginSettings(name: string, title?: string): void {
  if (!SHELL_SETTINGS_PLUGINS.has(name) || settingsPanel.value) return
  const plugin = plugins.value.find(item => item.name === name)
  settingsPanel.value = {
    host: name,
    plugin: name,
    title: String(title || `${plugin?.displayName || name}设置`),
    target: 'content',
    showAppearance: true,
  }
}

/** 隐藏 companion 不在 getPlugins() 的可见清单里，也没有 PluginChrome；按 native 面板
 *  上记录的 host / plugin 打开壳弹窗，schema、值与保存仍走该插件自己的 API。 */
function openNativeSettings(panel: { host: string; plugin: string; title: string }): void {
  if (settingsPanel.value) return
  settingsPanel.value = {
    host: panel.host,
    plugin: panel.plugin,
    title: `${panel.title || panel.plugin} 设置`,
    target: 'native',
    // 外观块写的是宿主页面的背景不透明度；native companion 没有自己的页面骨架，
    // 显示它只会让人误以为能单独调面板透明度。
    showAppearance: false,
  }
}

function onPluginChromeMessage(event: MessageEvent): void {
  if (event.origin !== window.location.origin) return
  const data = event.data
  if (!data || (
    data.type !== 'omnibox:ready'
    && data.type !== 'omnibox:chrome'
    && data.type !== 'omnibox:open-settings'
    && data.type !== 'omnibox:get-plugin-appearance'
    && data.type !== 'omnibox:set-plugin-alpha'
  )) return
  const name = frameNameForSource(event.source)
  if (!name) return
  if (data.type === 'omnibox:get-plugin-appearance') {
    postAppearance(name)
    return
  }
  if (data.type === 'omnibox:set-plugin-alpha') {
    if (data.follow === true) {
      setPluginAlpha(name, null)
    } else {
      const alpha = Number(data.alpha)
      if (Number.isFinite(alpha)) setPluginAlpha(name, alpha)
    }
    postAppearance(name)
    return
  }
  if (data.type === 'omnibox:open-settings') {
    // 内容页自己的设置入口（如歌词页右上角）请求壳打开弹窗：入口在内容里，
    // 弹窗仍然是页面级的，必须由壳渲染。
    openPluginSettings(name, typeof data.title === 'string' ? data.title : '')
    return
  }
  if (data.type === 'omnibox:ready') {
    postChromeView(name)
    return
  }
  const st = chromeStates[name]
  if (!st) return
  if (typeof data.title === 'string') st.title = data.title
  if (typeof data.subtitle === 'string') st.subtitle = data.subtitle
  if (typeof data.view === 'string') st.view = data.view
  if (data.badges && typeof data.badges === 'object') {
    const badges: Record<string, string> = {}
    Object.entries(data.badges).forEach(([key, value]) => {
      if (value !== null && value !== undefined && String(value) !== '') badges[key] = String(value)
    })
    st.badges = badges
  }
  if (data.status && typeof data.status === 'object') {
    st.status = {
      text: String(data.status.text || ''),
      state: String(data.status.state || 'idle'),
    }
  }
  if (data.actions && typeof data.actions === 'object') {
    st.actions = { ...(st.actions || {}), ...data.actions }
  }
  if (data.nav && typeof data.nav === 'object') {
    st.nav = { ...(st.nav || {}), ...data.nav }
  }
  if (data.search && typeof data.search === 'object') {
    st.search = { ...(st.search || {}), ...data.search }
  }
  if (Array.isArray(data.statusItems)) {
    st.statusItems = data.statusItems
  }
}

/** 宿主插件请求打开 native 面板：只接受已挂载 iframe 的同源消息，URL 必须在本仓库插件路径下。
 *  关闭由壳自己的入口负责（再次点击扩展项、切导航），内容页没有关闭消息。 */
function onNativeMessage(event: MessageEvent): void {
  if (event.origin !== window.location.origin) return
  const data = event.data
  if (!data || data.type !== 'omnibox:open-native') return
  const host = frameNameForSource(event.source)
  if (!host) return
  const plugin = String(data.plugin || '')
  const url = String(data.url || '')
  // hidden companion（image-cleaner / pixiv-sync）不在 system_get_plugins 的可见清单里，
  // 因此不能拿 getPlugins() 判存在；改为校验插件名形状 + URL 必须落在该插件目录下。
  // 来源已经限定为已挂载 iframe（frameNameForSource），同源页面只能打开本机插件目录。
  if (!isPluginPageUrl(plugin, url)) return
  if (settingsPanel.value?.target === 'native' && settingsPanel.value.host === host) {
    settingsPanel.value = null
  }
  nativePanel.value = {
    host,
    plugin,
    url,
    title: String(data.title || plugin),
    toolbar: [],
  }
}

/** freshness.js 是插件页组件；壳要复用它，先把 Bridge / Utils / Toast 三个入口垫上。
 *  它们只在组件调用时读取，所以安装时机只要早于 PluginChrome 挂载即可。 */
function installShellBridgeShims(): void {
  ;(window as any).Bridge = {
    callSystem: (method: string, ...args: unknown[]) => bridge.call(method, ...args),
  }
  ;(window as any).Utils = {
    iconHtml,
    // FolderPicker 在壳文档里渲染目录名/错误文本，需要与插件 iframe 同一套转义；
    // 直接复用 Vue 的共享实现，不在这里复制一份正则。
    escapeHtml: (value: unknown) => escapeHtmlText(value == null ? '' : String(value)),
  }
  ;(window as any).Toast = {
    success: (message: string) => toastSuccess(message),
    error: (message: string) => toastError(message),
    info: (message: string) => toastInfo(message),
    warning: (message: string) => toast(message, 'warning'),
    show: (message: string, type?: string) => toast(message, (type as any) || 'info'),
  }
  ensureIcons()
}

function onSetVarMessage(event: MessageEvent) {
  if (event.origin !== window.location.origin) return
  const data = event.data
  if (!data || data.type !== 'omnibox:set-var') return
  // 只接受本壳已挂载的 iframe：同源页面之间本来就能互相访问，这里是为了让
  // "谁写的"有答案，出问题时能定位到来源文档而不是一个匿名窗口。
  if (!isRegisteredFrameSource(event.source)) return
  commitAppearanceVar(data.key, data.value)
}

onMounted(async () => {
  // 壳偏好的写失败必须能看见：403（非管理员）与 5xx 都不能静默
  setShellPrefsNotifier(toastError)
  onShellPrefsChanged(() => {
    prefsTick.value += 1
    frameRefs.forEach((_frame, name) => postAppearance(name))
  })
  installShellBridgeShims()
  try {
    await loadPlugins()
    const plugins = getPlugins()
    plugins.forEach(p => {
      if (!p.chrome) return
      const first = p.chrome.nav?.[0]?.items?.[0]?.id || ''
      if (first) chromeViews[p.name] = first
      chromeStates[p.name] = {
        title: p.chrome.brand?.title || p.displayName,
        subtitle: p.chrome.brand?.subtitle || '',
        badges: {},
        status: { text: '正在读取…', state: 'idle' },
      }
    })
    plugins.forEach(p => {
      router.addRoute({
        path: p.route,
        name: p.name,
        component: { template: '<div></div>' },
        meta: { entryUrl: p.entryUrl, pluginName: p.name }
      })
    })
    if (route.path === '/') {
      // 首屏目标由「启动与窗口」偏好决定（默认回到上次离开的页面）
      router.replace(resolveStartRoute(plugins.map(p => p.route)))
    }
    // 供插件 iframe 内通用扩展入口调用：跳转到某个插件路由
    ;(window as any).__omniboxNavigate = (path: string) => router.push(path)

    isReady.value = true

    if (shellPrefs().startup.fullscreen) {
      // 桌面窗口：把窗口切到全屏。用"设为全屏"而不是"切换全屏" —— 切换是相对量，
      // 窗口已经是全屏时会把它切出去。浏览器访问时后端明确拒绝，忽略即可。
      try { await bridge.call('system_set_window_fullscreen', true) } catch { /* 非桌面模式 */ }
    }
  } catch (e: any) {
    error.value = e.message || '未知错误'
    console.error('插件加载失败:', e)
  }

  const fsObserver = new MutationObserver(() => {
    navHidden.value = document.documentElement.getAttribute('data-video-fullscreen') === 'true'
  })
  fsObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-video-fullscreen'] })

  window.addEventListener('omnibox:api-error', onApiError)
  window.addEventListener('message', onSetVarMessage)
  window.addEventListener('message', onPluginChromeMessage)
  window.addEventListener('message', onNativeMessage)
  window.addEventListener('visibilitychange', onVisibilityChange)
  window.addEventListener('beforeunload', onPageHide)
  window.addEventListener('pagehide', onPageHide)
  // 壳页面自己拖分隔条时（导航栏），splitter.js 走这个回调而不是 postMessage
  ;(window as any).__obxVarCommit = commitAppearanceVar
})

onUnmounted(() => {
  window.removeEventListener('omnibox:api-error', onApiError)
  window.removeEventListener('message', onSetVarMessage)
  window.removeEventListener('message', onPluginChromeMessage)
  window.removeEventListener('message', onNativeMessage)
  window.removeEventListener('visibilitychange', onVisibilityChange)
  window.removeEventListener('beforeunload', onPageHide)
  window.removeEventListener('pagehide', onPageHide)
  delete (window as any).__obxVarCommit
})

watch(
  () => route.meta.pluginName,
  (name) => {
    if (name && typeof name === 'string') {
      visitedPlugins[name] = true
      activePlugin.value = name
      if (nativePanel.value && nativePanel.value.host !== name) nativePanel.value = null
      if (settingsPanel.value && settingsPanel.value.host !== name) settingsPanel.value = null
      // 上次加载出错后重新进入：清除错误记录并强制重载 iframe，
      // 避免重新显示旧的错误标记页
      if (frameErrors[name]) {
        delete frameErrors[name]
        reloadCounters[name] = (reloadCounters[name] || 0) + 1
      }
    } else if (route.path === '/settings') {
      activePlugin.value = null
    } else if (route.path === '/status') {
      activePlugin.value = null
    }
    // 记下当前页面：下次启动若选「上次离开的页面」就回到这里。后端只把它放内存，
    // 安全退出（关窗 / 空闲兜底）时才写进 shell.json —— 强杀不算安全退出。
    if (name || route.path === '/settings') rememberLastRoute(route.path)
    // 切路由时把待写入的偏好一起落盘：去抖窗口正好被"用户切走了"截断时，
    // 上一处改动不该跟着页面一起留在内存里。
    void flushShellPrefs()
    // 路由切换后的可见性收敛：keep-alive 的 iframe 已存在（这里同步发通知），
    // 不保活的 iframe 由本次渲染挂载/卸载，交给 setFrameRef 与下面的
    // nextTick 兜底，保证两种形态都恰好收到一次 shown / hidden / dispose。
    refreshAllFrameVisibility()
    nextTick(() => {
      frameRefs.forEach((_frame, frameName) => {
        if (transientPlugins.value.some(p => p.name === frameName)) {
          frameMounted.set(frameName, activePlugin.value === frameName)
          notifyFrame(frameName, ensureFrame(frameName, activePlugin.value === frameName))
        }
      })
      disposeLeavingFrames(new Set(keepAlivePlugins.value.map(p => p.name)))
      refreshAllFrameVisibility()
      const activeName = activePlugin.value
      if (activeName && chromeViews[activeName]) postChromeView(activeName)
    })
  },
  { immediate: true }
)
</script>

<template>
  <div class="app-shell">
    <!-- 拖动分隔条：目标变量与设置页「导航栏宽度」是同一支 token，两者天然同源。
         min/max 与设置页滑块的 160-320 保持一致。 -->
    <aside
      class="nav-sidebar" :class="{ 'nav-hidden': navHidden }"
      data-obx-splitter="--nav-width"
      data-obx-splitter-min="160"
      data-obx-splitter-max="320"
      data-obx-splitter-default="200"
    >
      <div class="logo">OmniBox</div>
      <nav v-if="isReady">
        <template v-if="plugins.length > 0">
          <div
            v-for="p in plugins" :key="p.name"
            class="nav-item" :class="{ active: currentPlugin === p.name }"
            @click="router.push(p.route)"
          >
            <Icon :name="p.icon" class="icon" />
            <span class="text">{{ p.displayName }}</span>
          </div>
        </template>
        <div v-else-if="error" class="error">{{ error }}</div>
        <div v-else class="hint">暂无插件</div>
        <div class="nav-divider"></div>
        <div
          class="nav-item" :class="{ active: isSettings }"
          @click="router.push('/settings')"
        >
          <Icon name="icon:settings" class="icon" />
          <span class="text">设置</span>
        </div>
      </nav>
    </aside>
    <main class="main-view">
      <template v-if="isReady">
        <template v-for="p in keepAlivePlugins" :key="p.name">
          <div
            v-if="visitedPlugins[p.name]" v-show="activePlugin === p.name" class="plugin-frame-container"
            :ref="el => setFrameRef(p.name, el as Element | null)"
          >
            <PluginChrome
              v-if="p.chrome"
              :plugin="p"
              :view="chromeViews[p.name]"
              :state="chromeStates[p.name]"
              :alpha="pluginAlphaFor(p.name)"
              :native-open="nativePanel?.host === p.name"
              :title-override="nativePanel?.host === p.name ? nativePanel.title : ''"
              :native-actions="nativePanel?.host === p.name ? nativePanel.toolbar : []"
              @view="onChromeView(p.name, $event)"
              @action="onChromeAction(p.name, $event)"
              @query="onChromeQuery(p.name, $event)"
              @native="onChromeNative(p.name, $event)"
              @native-action="onNativeToolbarAction(p.name, $event)"
            >
              <iframe
                v-show="nativePanel?.host !== p.name"
                :src="pluginSrc(p)"
                :data-plugin-name="p.name"
                frameborder="0"
                class="plugin-iframe"
                allow="fullscreen *"
                @load="onFrameLoad($event, p.name)"
              ></iframe>
              <NativePanel
                v-if="nativePanel && nativePanel.host === p.name"
                :ref="el => setNativeRef(p.name, el)"
                :plugin="nativePanel.plugin"
                :url="nativePanel.url"
                :title="nativePanel.title"
                :alpha="pluginAlphaFor(nativePanel.host)"
                @toolbar="onNativeToolbar(p.name, $event)"
              />
            </PluginChrome>
            <iframe
              v-else
              :src="pluginSrc(p)"
              :data-plugin-name="p.name"
              frameborder="0"
              class="plugin-iframe"
              allow="fullscreen *"
              @load="onFrameLoad($event, p.name)"
            ></iframe>
          </div>
        </template>
        <template v-for="p in transientPlugins" :key="p.name">
          <div
            v-if="activePlugin === p.name" class="plugin-frame-container"
            :ref="el => setFrameRef(p.name, el as Element | null)"
          >
            <PluginChrome
              v-if="p.chrome"
              :plugin="p"
              :view="chromeViews[p.name]"
              :state="chromeStates[p.name]"
              :alpha="pluginAlphaFor(p.name)"
              :native-open="nativePanel?.host === p.name"
              :title-override="nativePanel?.host === p.name ? nativePanel.title : ''"
              :native-actions="nativePanel?.host === p.name ? nativePanel.toolbar : []"
              @view="onChromeView(p.name, $event)"
              @action="onChromeAction(p.name, $event)"
              @query="onChromeQuery(p.name, $event)"
              @native="onChromeNative(p.name, $event)"
              @native-action="onNativeToolbarAction(p.name, $event)"
            >
              <iframe
                v-show="nativePanel?.host !== p.name"
                :src="pluginSrc(p)"
                :data-plugin-name="p.name"
                frameborder="0"
                class="plugin-iframe"
                allow="fullscreen *"
                @load="onFrameLoad($event, p.name)"
              ></iframe>
              <NativePanel
                v-if="nativePanel && nativePanel.host === p.name"
                :ref="el => setNativeRef(p.name, el)"
                :plugin="nativePanel.plugin"
                :url="nativePanel.url"
                :title="nativePanel.title"
                :alpha="pluginAlphaFor(nativePanel.host)"
                @toolbar="onNativeToolbar(p.name, $event)"
              />
            </PluginChrome>
            <iframe
              v-else
              :src="pluginSrc(p)"
              :data-plugin-name="p.name"
              frameborder="0"
              class="plugin-iframe"
              allow="fullscreen *"
              @load="onFrameLoad($event, p.name)"
            ></iframe>
          </div>
        </template>
        <SettingsView v-show="isSettings" />
        <StatusView v-show="isStatus" @retry="handleRetry" />
      </template>
      <div v-else-if="error" class="loading">插件加载失败: {{ error }}</div>
      <div v-else class="loading">框架加载中...</div>
    </main>
    <PluginSettingsModal
      v-if="settingsPanel"
      :plugin="settingsPanel.plugin"
      :title="settingsPanel.title"
      :show-appearance="settingsPanel.showAppearance !== false"
      @close="settingsPanel = null"
      @saved="onSettingsSaved"
    />
  </div>
</template>

<style scoped>
.plugin-frame-container {width: 100%;height: 100%;border: none;outline: none;}
.plugin-iframe {width: 100%;height: 100%;border: 0;outline: none;display: block;}
</style>
