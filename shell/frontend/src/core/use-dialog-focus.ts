import { nextTick, onBeforeUnmount, onMounted, type Ref } from 'vue'

/**
 * 壳内页面级弹窗的焦点契约。
 *
 * - 打开时记住触发元素，焦点进入弹窗；关闭后归还。
 * - 把弹窗之外的直接兄弟节点设为 inert，Tab 不进入背景。
 * - Tab / Shift+Tab 在弹窗内循环，Esc 在任何焦点下都走 onClose。
 *
 * 只处理壳文档中的弹窗（设置弹窗）；内容 iframe 的 modal 仍由插件自己管理。
 */
export interface DialogFocusOptions {
  container: Ref<HTMLElement | null>
  overlay: Ref<HTMLElement | null>
  onClose: () => void
}

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

/**
 * 壳的设置弹窗里还会再开一层目录选择器（FolderPicker 的三个 `.iv-picker-modal`）。
 * 焦点循环必须停在最上层那层，否则第一次 Tab 就回到被遮住的设置表单上：
 * 无字段可聚焦时甚至会落到背景的关闭按钮，Enter 一下就把整个设置弹窗关掉。
 */
const NESTED_DIALOG_SELECTOR = '.iv-picker-modal'

export function useDialogFocus(options: DialogFocusOptions) {
  let previousFocus: HTMLElement | null = null
  const inertSiblings: Array<{ element: Element; hadInert: boolean }> = []

  /** 当前的焦点作用域：最上层嵌套弹窗，没有嵌套时就是设置弹窗本身。 */
  function focusScope(): HTMLElement | null {
    const container = options.container.value
    if (!container) return null
    const nested = container.querySelectorAll<HTMLElement>(NESTED_DIALOG_SELECTOR)
    return nested.length ? nested[nested.length - 1] : container
  }

  function focusableElements(): HTMLElement[] {
    const scope = focusScope()
    if (!scope) return []
    return Array.from(scope.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
      .filter(element => element.tabIndex >= 0 && element.getClientRects().length > 0)
  }

  function focusFallback(): void {
    const scope = focusScope()
    if (scope && scope !== options.container.value) {
      scope.querySelector<HTMLElement>('.modal-box')?.focus()
      return
    }
    closeButton()?.focus()
  }

  function closeButton(): HTMLElement | null {
    return options.container.value?.querySelector<HTMLElement>('.settings-modal-close') || null
  }

  /** 等 schema 渲染完后调用：优先第一个可编辑字段，其次关闭按钮。 */
  function focusFirstField(): void {
    const container = options.container.value
    if (!container) return
    // 已有嵌套弹窗时焦点属于上层，不能再抢回来。
    if (focusScope() !== container) return
    const editable = container.querySelector<HTMLElement>([
      'input:not([disabled]):not([type="hidden"]):not([readonly])',
      'select:not([disabled])',
      'textarea:not([disabled])',
    ].join(','))
    ;(editable || closeButton())?.focus()
  }

  function setBackgroundInert(on: boolean): void {
    const overlay = options.overlay.value
    const parent = overlay?.parentElement
    if (!overlay || !parent) return
    if (on) {
      Array.from(parent.children).forEach(element => {
        if (element === overlay) return
        inertSiblings.push({ element, hadInert: element.hasAttribute('inert') })
        element.setAttribute('inert', '')
      })
      return
    }
    inertSiblings.forEach(({ element, hadInert }) => {
      if (hadInert) element.setAttribute('inert', '')
      else element.removeAttribute('inert')
    })
    inertSiblings.length = 0
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      options.onClose()
      return
    }
    if (event.key !== 'Tab') return

    const elements = focusableElements()
    if (!elements.length) {
      event.preventDefault()
      focusFallback()
      return
    }

    const active = document.activeElement as HTMLElement | null
    const index = active ? elements.indexOf(active) : -1
    if (index === -1) {
      event.preventDefault()
      ;(event.shiftKey ? elements[elements.length - 1] : elements[0]).focus()
      return
    }
    if (!event.shiftKey && index === elements.length - 1) {
      event.preventDefault()
      elements[0].focus()
      return
    }
    if (event.shiftKey && index === 0) {
      event.preventDefault()
      elements[elements.length - 1].focus()
    }
  }

  onMounted(() => {
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    setBackgroundInert(true)
    // 先让焦点进入弹窗；schema 加载完成后再由 focusFirstField() 移到第一个字段。
    closeButton()?.focus()
    document.addEventListener('keydown', onKeydown, true)
  })

  onBeforeUnmount(() => {
    document.removeEventListener('keydown', onKeydown, true)
    setBackgroundInert(false)
    const target = previousFocus
    previousFocus = null
    if (target && target.isConnected) {
      // 等一帧，避免父组件在同一次事件里继续处理点击导致焦点被抢走。
      void nextTick(() => target.focus())
    }
  })

  return { focusFirstField }
}
