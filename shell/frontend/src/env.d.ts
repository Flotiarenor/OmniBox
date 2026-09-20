/// <reference types="vite/client" />
declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

interface PyWebViewAPI {
  system_get_plugins(): Promise<any[]>
  system_get_config(): Promise<any>
  system_get_plugin_status(): Promise<{ loaded: string[]; failures: { name: string; reason: string }[] }>
  system_get_shell_info(): Promise<any>
  system_toggle_fullscreen(): Promise<{ success: boolean; error?: string }>
  [key: string]: (...args: any[]) => Promise<any>
}

/** 构建时由 vite.config.ts 注入（来源 shell/frontend/package.json 的 version）。 */
declare const __APP_VERSION__: string

interface Window {
  pywebview?: { api: PyWebViewAPI }
}
