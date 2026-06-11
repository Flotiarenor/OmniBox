/// <reference types="vite/client" />
declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

interface PyWebViewAPI {
  system_get_plugins(): Promise<any[]>
  system_get_config(): Promise<any>
  [key: string]: (...args: any[]) => Promise<any>
}

interface Window {
  pywebview?: { api: PyWebViewAPI }
}