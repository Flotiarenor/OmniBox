// Shell 外观启动恢复：主题 + 自定义 CSS 变量
export type Theme = 'light' | 'dark'

const THEME_STORAGE_KEY = 'omni-theme'
const COLORS_STORAGE_KEY = 'omni-custom-colors'

export function getStoredTheme(): Theme {
  const stored = localStorage.getItem(THEME_STORAGE_KEY)
  if (stored === 'light' || stored === 'dark') return stored

  // 没有保存过主题时，沿用 index.html 上已有的 data-theme（当前默认 dark）。
  return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark'
}

export function applyStoredAppearance(): void {
  // 1. 恢复主题。没有历史记录时沿用 index.html 上的 data-theme 默认值。
  document.documentElement.setAttribute('data-theme', getStoredTheme())

  // 2. 恢复自定义颜色，并写回 data-custom-colors，
  //    这样插件 iframe 的注入脚本才能通过 MutationObserver 同步到。
  const savedColors = localStorage.getItem(COLORS_STORAGE_KEY)
  if (savedColors) {
    try {
      const map = JSON.parse(savedColors) as Record<string, string>
      Object.entries(map).forEach(([key, value]) => {
        document.documentElement.style.setProperty(key, value)
      })
      document.documentElement.setAttribute('data-custom-colors', savedColors)
      return
    } catch {
      // 存储内容损坏时按未配置处理。
    }
  }
  document.documentElement.removeAttribute('data-custom-colors')
}

export function setStoredTheme(theme: Theme): void {
  document.documentElement.setAttribute('data-theme', theme)
  localStorage.setItem(THEME_STORAGE_KEY, theme)
}

export function persistCustomColors(colors: Record<string, string>): void {
  const json = JSON.stringify(colors)
  localStorage.setItem(COLORS_STORAGE_KEY, json)
  document.documentElement.setAttribute('data-custom-colors', json)
}
