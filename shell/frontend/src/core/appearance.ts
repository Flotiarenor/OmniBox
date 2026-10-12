// Shell 外观启动恢复：主题 + 自定义 CSS 变量
//
// 持久化已经不在 localStorage，而在壳偏好（`shell-prefs.ts`，落盘于
// `<config_dir>/shell.json`）：本模块只负责"把偏好画到 html 上"与"校验外来写入"。
import {
  colorsJson, shellPrefs, setShellTheme, setShellVar,
  type Theme,
} from './shell-prefs'
import { isStorableVarValue, isValidVarName, isValidVarValue } from './var-rules'

export type { Theme }
// 形状判据住在 var-rules.ts（偏好通路也要用同一份），这里转出去给既有调用方
export { isValidVarName, isValidVarValue }

/** 外观变量写入后广播的事件。
 *
 * 写入方有两处：设置页的样式面板，以及拖动分隔条（splitter）后的落盘。两边都写
 * 同一份偏好快照与同一个 html 内联，谁都不该拿自己那份过期快照整段覆盖对方 ——
 * 所以统一走 `setAppearanceVar()`（读-改-写），并靠这个事件把新值回灌给已挂载的
 * 界面（设置页是 v-show 常驻的，不重挂载）。
 */
export const APPEARANCE_CHANGED = 'omnibox:appearance-changed'

/** 写入单个外观变量：改偏好 → 改 html 内联 → 广播。
 *
 * 两条写入路径（设置页面板、拖动分隔条）都经过这里，插件的 iframe 由
 * `data-custom-colors` 属性变化自动跟随，不需要为分隔条另开同步机制。
 */
export function setAppearanceVar(key: string, value: string): boolean {
  if (!isValidVarName(key) || !isStorableVarValue(value)) return false
  setShellVar(key, value)
  document.documentElement.style.setProperty(key, value)
  document.documentElement.setAttribute('data-custom-colors', colorsJson())
  window.dispatchEvent(new CustomEvent(APPEARANCE_CHANGED, { detail: { key, value } }))
  return true
}

export function getStoredTheme(): Theme {
  return shellPrefs().theme
}

/** 把偏好里的主题与自定义变量画到 html 上（首帧之前调用一次，之后由偏好变更驱动）。 */
export function applyStoredAppearance(): void {
  const prefs = shellPrefs()
  document.documentElement.setAttribute('data-theme', prefs.theme)
  const colors = colorsJson()
  Object.entries(prefs.colors).forEach(([key, value]) => {
    if (isValidVarName(key)) document.documentElement.style.setProperty(key, value)
  })
  // 写回 data-custom-colors：插件 iframe 的注入脚本靠 MutationObserver 同步它
  document.documentElement.setAttribute('data-custom-colors', colors)
}

export function setStoredTheme(theme: Theme): void {
  document.documentElement.setAttribute('data-theme', theme)
  setShellTheme(theme)
}
