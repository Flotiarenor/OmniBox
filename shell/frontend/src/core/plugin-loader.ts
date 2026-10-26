import { shallowRef } from 'vue'
import { useBridge } from './bridge'

export interface PluginChromeNavItem {
  id: string
  label: string
  icon?: string
  badge?: string
  visible?: boolean
}

export interface PluginChromeNavSection {
  id?: string
  section?: string
  dynamic?: boolean
  visible?: boolean
  items: PluginChromeNavItem[]
}

export type PluginChromeActionVariant = 'default' | 'primary' | 'danger'
export type PluginChromeActionKind = 'button' | 'text' | 'badge'

export interface PluginChromeToolbarAction {
  id: string
  label: string
  icon?: string
  variant?: PluginChromeActionVariant
  kind?: PluginChromeActionKind
  /** leading：标题左侧，始终直接可见；trailing（默认）：右侧动作区，可进 More。 */
  placement?: 'leading' | 'trailing'
  visible?: boolean
  disabled?: boolean
  active?: boolean
}

export interface PluginChromeSearch {
  placeholder?: string
  enabled?: boolean
  /**
   * 内容页希望搜索框显示的词。
   *
   * 壳是搜索框的唯一持有者，内容页切视图/进二级页时把自己的关键词清掉后，
   * 必须能让输入框跟着回到空 —— 否则"上次搜的词"留在框里，与列表内容不一致。
   * 只在内容页主动上报时同步：壳自己的输入仍以输入框为准，避免打字被回冲。
   */
  value?: string
  /**
   * 内容页要求清空输入框。取递增序号：两次清空之间可能没有新的 chrome 上报，
   * 布尔值会让第二次清空无法触发。壳只在序号变化时执行一次。
   */
  clear?: number | boolean
}

export type PluginChromeStatusKind = 'text' | 'dot-text' | 'eq-text'

export interface PluginChromeStatusItem {
  id: string
  text: string
  state?: string
}

export interface PluginChromeStatusSpec {
  kind?: PluginChromeStatusKind
  items?: PluginChromeStatusItem[]
}

export interface PluginChromeBrand {
  icon?: string
  title?: string
  subtitle?: string
  accent?: string
}

export type PluginContentMode = 'canvas' | 'native' | 'hybrid'

export interface PluginChromeExtensionsSpec {
  placement?: 'sidebar'
  title?: string
}

export interface PluginChromeSpec {
  brand: PluginChromeBrand
  nav: PluginChromeNavSection[]
  /** 侧栏 section 默认顺序；键为 `nav:<组id>` 与 `extensions`，缺省按 nav 数组序、扩展收尾。 */
  sidebarOrder?: string[]
  toolbar?: PluginChromeToolbarAction[]
  search?: PluginChromeSearch
  status?: boolean | PluginChromeStatusSpec
  freshness?: boolean | { unit?: string }
  extensions?: PluginChromeExtensionsSpec
  content?: PluginContentMode
}

export interface PluginChromeActionState {
  visible?: boolean
  disabled?: boolean
  active?: boolean
  label?: string
  variant?: PluginChromeActionVariant
  kind?: PluginChromeActionKind
}

export interface PluginChromeNavState {
  visible?: boolean
  badge?: string
  items?: PluginChromeNavItem[]
}

export interface PluginChromeState {
  title: string
  subtitle: string
  badges: Record<string, string>
  status: { text: string; state: string }
  /** 内容页主动指定/清除高亮（阅读态、二级页）；壳点击导航时会先清掉它。 */
  view?: string
  actions?: Record<string, PluginChromeActionState>
  nav?: Record<string, PluginChromeNavState>
  search?: PluginChromeSearch
  statusItems?: PluginChromeStatusItem[]
}

export interface PluginManifest {
  name: string
  displayName: string
  icon: string
  route: string
  entryUrl: string
  keepAlive?: boolean
  chrome?: PluginChromeSpec | null
}

// 必须是响应式容器，不能是普通数组。
//
// 背景：App.vue 用 `computed(() => getPlugins())` 渲染导航。若这里返回普通数组，
// computed 没有任何响应式依赖 —— 一旦在数据到达前被求值（例如 setup 期某个
// immediate watcher 触发了第一次求值），就会永久缓存住空数组，之后接口返回的
// 插件列表再也不会反映到界面上：表现为"导航只有设置、其余插件全部消失"，而且
// 控制台没有任何报错（这是最费时间的一类缺陷）。
const _plugins = shallowRef<PluginManifest[]>([])

export async function loadPlugins() {
  const bridge = useBridge()
  _plugins.value = await bridge.call('system_get_plugins')
  return _plugins.value
}

export function getPlugins() {
  return _plugins.value
}
