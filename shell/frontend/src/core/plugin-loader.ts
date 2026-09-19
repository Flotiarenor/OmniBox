import { shallowRef } from 'vue'
import { useBridge } from './bridge'

export interface PluginManifest {
  name: string
  displayName: string
  icon: string
  route: string
  entryUrl: string
  keepAlive?: boolean
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
