import { useBridge } from './bridge'

export interface PluginManifest {
  name: string
  displayName: string
  icon: string
  route: string
  entryUrl: string
  destroyOnLeave?: boolean
}

let _plugins: PluginManifest[] = []

export async function loadPlugins() {
  const bridge = useBridge()
  _plugins = await bridge.call('system_get_plugins')
  return _plugins
}

export function getPlugins() {
  return _plugins
}