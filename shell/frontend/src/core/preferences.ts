// Shell 启动 / 窗口偏好与「上次离开的页面」的入口。
//
// 存储与去抖都住在 `shell-prefs.ts`（后端 `<config_dir>/shell.json` 的 prefs 段），
// 本模块只做转发：调用方（App.vue / SettingsView.vue）不必知道偏好存在哪里。
//
// 这里**不再有** localStorage 版本：桌面窗口的 localStorage 会在 LevelDB 日志被
// 强杀截断后整段失效，壳偏好因此搬进了配置文件（见 .dsh/shell-prefs-report.md）。
import { shellPrefs, type StartupPrefs } from './shell-prefs'

export {
  rememberShellRoute as rememberLastRoute,
  resolveStartRoute,
  setShellStartup as setStartupPrefs,
  type StartPage,
  type StartupPrefs,
} from './shell-prefs'

/** 当前启动偏好（副本：调用方改它不会影响存储，写入走 setStartupPrefs）。 */
export function getStartupPrefs(): StartupPrefs {
  return { ...shellPrefs().startup }
}
