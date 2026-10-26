<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor-->
<script setup lang="ts">
/**
 * 壳渲染的插件页面骨架。
 *
 * 插件 iframe 只装内容；品牌块、左栏、导航、搜索、顶栏、状态栏与分隔条都在这里。
 * 插件通过消息上报标题/徽标/动作/导航/搜索/状态，壳只渲染声明与补丁，不解释业务。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import PluginExtensions from './PluginExtensions.vue'
import type { SchemaAction } from '../core/plugin-content'
import type {
  PluginChromeNavItem,
  PluginChromeSpec,
  PluginChromeState,
  PluginChromeToolbarAction,
  PluginManifest,
} from '../core/plugin-loader'

const props = defineProps<{
  plugin: PluginManifest
  view?: string
  state?: PluginChromeState
  /** 页面背景不透明度（30–100）；缺省时退到壳的全局值。 */
  alpha?: number
  /** 本插件的 native 面板是否打开；关闭时清掉扩展入口的选中态。 */
  nativeOpen?: boolean
  /** native 面板的标题覆盖（显示在宿主顶栏，不再单独画 native 头部）。 */
  titleOverride?: string
  /** native 逻辑页上报的页面级动作，在宿主顶栏渲染。 */
  nativeActions?: SchemaAction[]
}>()

const emit = defineEmits<{
  (e: 'view', id: string): void
  (e: 'action', id: string): void
  (e: 'query', value: string): void
  (e: 'native', payload: { plugin: string; url: string; title: string }): void
  (e: 'native-action', action: { id: string; payload?: unknown }): void
}>()

interface ExtensionEntry {
  id?: string
  label?: string
  plugin?: string
  view?: string
  native?: { url?: string; title?: string } | null
}

const chrome = computed<PluginChromeSpec>(
  () => props.plugin.chrome || { brand: {}, nav: [] },
)
const brand = computed(() => chrome.value.brand || {})
const ui = computed<PluginChromeState>(() => props.state || {
  title: brand.value.title || props.plugin.displayName,
  subtitle: brand.value.subtitle || '',
  badges: {},
  status: { text: '正在读取…', state: 'idle' },
})
const pageStyle = computed<Record<string, string>>(() => ({
  '--plugin-brand-2': brand.value.accent || 'var(--accent)',
  '--plugin-glass-alpha': `${props.alpha ?? 84}%`,
}))

/** 运行期动作补丁：按 id 合并到声明上，再过滤掉 visible=false。 */
const actions = computed<PluginChromeToolbarAction[]>(() => {
  const overrides = ui.value.actions || {}
  return (chrome.value.toolbar || [])
    .map(action => ({ ...action, ...(overrides[action.id] || {}) }))
    .filter(action => action.visible !== false)
})

/** leading 固定渲染在标题左侧，不参与右侧溢出测量与 More 菜单。 */
const leadingActions = computed(() => actions.value.filter(action => action.placement === 'leading'))
const trailingActions = computed(() => actions.value.filter(action => action.placement !== 'leading'))

/** 运行期导航补丁：组可按 group id / 序号替换 items 或整体显隐；项按 item id 显隐/改徽标。 */
const navGroups = computed(() => (chrome.value.nav || []).map((group, index) => {
  const key = group.id || String(index)
  const groupPatch = (ui.value.nav || {})[key] || {}
  const items = (groupPatch.items || group.items || [])
    .map((item: PluginChromeNavItem) => {
      const patch = (ui.value.nav || {})[item.id] || {}
      return {
        ...item,
        badge: patch.badge !== undefined
          ? patch.badge
          : (ui.value.badges?.[item.id] !== undefined ? ui.value.badges[item.id] : item.badge),
        visible: patch.visible !== undefined ? patch.visible : item.visible,
      }
    })
    .filter(item => item.visible !== false)
  return {
    key,
    sectionKey: 'nav:' + key,
    section: group.section,
    visible: groupPatch.visible !== undefined ? groupPatch.visible : group.visible,
    items,
  }
}).filter(group => group.visible !== false && group.items.length > 0))

/** 侧栏 section：导航组 + 扩展区，按 manifest 的 sidebarOrder 排序。 */
const sidebarSections = computed(() => {
  type SidebarSection =
    | { kind: 'nav'; key: string; group: (typeof navGroups.value)[number] }
    | { kind: 'extensions'; key: string; title?: string }
  const navs: SidebarSection[] = navGroups.value.map(group => ({ kind: 'nav', key: group.sectionKey, group }))
  const sections: SidebarSection[] = [...navs]
  if (chrome.value.extensions) {
    sections.push({
      kind: 'extensions',
      key: 'extensions',
      title: chrome.value.extensions.title,
    })
  }
  const byKey = new Map(sections.map(section => [section.key, section]))
  const defaults = (chrome.value.sidebarOrder || [])
    .filter(key => byKey.has(key))
  for (const section of sections) {
    if (!defaults.includes(section.key)) defaults.push(section.key)
  }
  return defaults.map(key => byKey.get(key)!)
})

const search = computed(() => ({
  placeholder: '搜索…',
  ...(chrome.value.search || {}),
  ...(ui.value.search || {}),
}))
const showSearch = computed(() => !!chrome.value.search && search.value.enabled !== false)
const query = ref('')
const searchExpanded = ref(false)
let queryTimer = 0
async function toggleSearch(): Promise<void> {
  searchExpanded.value = !searchExpanded.value
  if (!searchExpanded.value) return
  await nextTick()
  searchRef.value?.querySelector('input')?.focus()
}
function stopQueryTimer(): void {
  if (queryTimer) window.clearTimeout(queryTimer)
  queryTimer = 0
}
function onQueryInput(): void {
  stopQueryTimer()
  queryTimer = window.setTimeout(() => emit('query', query.value.trim()), 250)
}

// 内容页要求清空输入框：`clear` 是递增序号，壳只在序号变化时执行一次。
// 用序号而不是布尔，是因为内容页可能在两次清空之间没有新的 chrome 上报。
let lastSearchClear = 0
watch(() => search.value.clear, (clear) => {
  if (clear === undefined || clear === null || clear === false) return
  const seq = typeof clear === 'number' ? clear : 1
  if (seq <= lastSearchClear) return
  lastSearchClear = seq
  stopQueryTimer()
  query.value = ''
})

// 内容页显式给出的输入值：只在内容页主动回填时上报（入口重置/切视图），
// 壳自己的输入不回声，因此这里可以无条件对齐。
watch(() => search.value.value, (value) => {
  if (typeof value !== 'string' || value === query.value) return
  stopQueryTimer()
  query.value = value
})

const statusSpec = computed(() => (
  typeof chrome.value.status === 'object' && chrome.value.status ? chrome.value.status : {}
))
const showStatus = computed(() => chrome.value.status !== undefined && chrome.value.status !== false)
// 迷你 EQ：状态声明为 eq-text 且内容页上报播放中（state=ok）时，用四柱动画代替状态点。
// data-status-kind 把声明值也写进 DOM，便于审计工具区分"声明是 dot-text"与"声明没生效"。
const statusKind = computed(() => statusSpec.value.kind || 'dot-text')
const statusEq = computed(() => (
  statusKind.value === 'eq-text' && ui.value.status.state === 'ok'
))
const statusItems = computed(() => (
  ui.value.statusItems?.length ? ui.value.statusItems : (statusSpec.value.items || [])
))
const activeExtensionId = ref('')
const activeView = computed(() => (
  activeExtensionId.value
    ? ''
    : (ui.value.view !== undefined ? ui.value.view : (props.view || ''))
))

function extensionKey(entry: ExtensionEntry): string {
  return entry.id || entry.view || entry.plugin || ''
}

function badgeFor(item: PluginChromeNavItem): string {
  return item.badge || ''
}

function actionClass(action: PluginChromeToolbarAction): Record<string, boolean> {
  return {
    'btn-primary': action.variant === 'primary',
    'btn-danger': action.variant === 'danger',
    active: !!action.active,
    'plugin-action-disabled': !!action.disabled,
  }
}

function runAction(action: PluginChromeToolbarAction): void {
  if (action.disabled) return
  emit('action', action.id)
}

// ---- 顶栏动作溢出 ----
// 标题至少保留 160px；搜索/freshness/文本徽标是固定项；按钮按优先级塞进剩余宽度，
// 放不下的进「更多」菜单。菜单是页面内 DOM，不改插件 action id/visible/disabled/active。
const toolbarRef = ref<HTMLElement | null>(null)
const leadingRef = ref<HTMLElement | null>(null)
const actionsRef = ref<HTMLElement | null>(null)
const measureRef = ref<HTMLElement | null>(null)
const moreMeasureRef = ref<HTMLElement | null>(null)
const searchRef = ref<HTMLElement | null>(null)
const freshnessRef = ref<HTMLElement | null>(null)
const moreRef = ref<HTMLElement | null>(null)
const moreButtonRef = ref<HTMLButtonElement | null>(null)
const menuRef = ref<HTMLElement | null>(null)
const visibleActionIndexes = ref<number[]>([])
const overflowActionIndexes = ref<number[]>([])
const menuOpen = ref(false)
const toolbarCompact = ref(false)
const toolbarTight = ref(false)
const toolbarUltra = ref(false)
let measureTimer = 0
let toolbarObserver: ResizeObserver | null = null

const measureCandidates = computed(() => trailingActions.value
  .map((action, index) => ({ action, index }))
  .filter(({ action }) => action.kind !== 'text' && action.kind !== 'badge'))

function isFixedAction(action: PluginChromeToolbarAction): boolean {
  return action.kind === 'text' || action.kind === 'badge'
}

function actionPriority(index: number): number {
  const action = trailingActions.value[index]
  if (!action) return 0
  if (action.id === 'back') return 110
  if (action.active || action.variant === 'primary') return 100
  if (action.id === 'multi-select') return 90
  if (action.id === 'settings') return 70
  if (action.variant === 'danger') return 60
  if (action.id === 'refresh') return 40
  return 10
}

function scheduleMeasure(): void {
  if (measureTimer) return
  measureTimer = window.setTimeout(() => {
    measureTimer = 0
    void measureActions()
  }, 0)
}

async function measureActions(): Promise<void> {
  await nextTick()
  const toolbar = toolbarRef.value
  const actionsBox = actionsRef.value
  const measureBox = measureRef.value
  if (!toolbar || !actionsBox || !measureBox) return

  // 先按真实 toolbar 宽度切紧凑档位；档位会改变搜索/freshness/标签宽度，
  // 因此切换后必须再渲染一帧，随后才读宽度。
  const nextCompact = toolbar.clientWidth < 900
  const nextTight = toolbar.clientWidth < 600
  const nextUltra = toolbar.clientWidth < 300
  if (nextCompact !== toolbarCompact.value
      || nextTight !== toolbarTight.value
      || nextUltra !== toolbarUltra.value) {
    toolbarCompact.value = nextCompact
    toolbarTight.value = nextTight
    toolbarUltra.value = nextUltra
    await nextTick()
    if (!toolbar.isConnected) return
  }

  const widths = new Map<number, number>()
  measureBox.querySelectorAll<HTMLElement>('[data-measure-action]').forEach(node => {
    const index = Number(node.dataset.measureAction)
    if (Number.isFinite(index)) widths.set(index, node.getBoundingClientRect().width)
  })

  const fixedIndexes = trailingActions.value
    .map((action, index) => ({ action, index }))
    .filter(({ action }) => isFixedAction(action))
    .map(({ index }) => index)
  const fixedWidth = fixedIndexes.reduce((sum, index) => {
    const node = actionsBox.querySelector<HTMLElement>(`[data-action-index="${index}"]`)
    return sum + (node ? node.getBoundingClientRect().width : 0)
  }, 0)
  const searchWidth = searchRef.value ? searchRef.value.getBoundingClientRect().width : 0
  const freshnessWidth = freshnessRef.value ? freshnessRef.value.getBoundingClientRect().width : 0
  const moreWidth = moreMeasureRef.value
    ? moreMeasureRef.value.getBoundingClientRect().width
    : 76
  // 折叠/隐藏后的零宽元素不再占 flex gap，固定项计数必须按真实宽度算。
  const fixedCount = fixedIndexes.length
    + (searchWidth > 0 ? 1 : 0)
    + (freshnessWidth > 0 ? 1 : 0)
  const baseWidth = fixedWidth + searchWidth + freshnessWidth
  const candidates = measureCandidates.value.map(item => item.index)
  if (!candidates.length) {
    visibleActionIndexes.value = fixedIndexes
    overflowActionIndexes.value = []
    menuOpen.value = false
    return
  }

  const compact = toolbar.clientWidth < 800
  const leadingWidth = leadingRef.value ? leadingRef.value.getBoundingClientRect().width : 0
  const heading = toolbar.querySelector<HTMLElement>('.plugin-heading')
  const headingMin = heading
    ? Number.parseFloat(getComputedStyle(heading).minWidth) || 64
    : 64
  // 按 toolbar 的真实宽度测量，而不是 viewport：侧栏/分栏改变时剩余宽度也会变。
  const toolbarStyle = getComputedStyle(toolbar)
  const paddingX = (Number.parseFloat(toolbarStyle.paddingLeft) || 0)
    + (Number.parseFloat(toolbarStyle.paddingRight) || 0)
  const toolbarGap = Number.parseFloat(toolbarStyle.columnGap) || 12
  const available = Math.max(
    0,
    toolbar.clientWidth - paddingX - leadingWidth - headingMin
      - toolbarGap * (leadingWidth ? 2 : 1),
  )
  if (!available) {
    visibleActionIndexes.value = [
      ...fixedIndexes,
      ...(compact && candidates.length
        ? [[...candidates].sort((a, b) => (
          actionPriority(b) - actionPriority(a) || a - b
        ))[0]]
        : candidates),
    ].sort((a, b) => a - b)
    overflowActionIndexes.value = candidates.filter(index => !visibleActionIndexes.value.includes(index))
    return
  }

  const totalFor = (visible: Set<number>): number => {
    const overflow = visible.size < candidates.length
    const count = fixedCount + visible.size + (overflow ? 1 : 0)
    if (!count) return 0
    const actionGap = Number.parseFloat(getComputedStyle(actionsBox).columnGap) || 8
    let width = baseWidth + (overflow ? moreWidth : 0)
    visible.forEach(index => { width += widths.get(index) || 0 })
    return width + actionGap * (count - 1)
  }

  let visible = new Set<number>(candidates)
  if (compact) {
    const kept = [...candidates].sort((a, b) => (
      actionPriority(b) - actionPriority(a) || a - b
    ))[0]
    visible = new Set(kept === undefined ? [] : [kept])
  }
  while (visible.size && totalFor(visible) > available) {
    const drop = [...visible].sort((a, b) => (
      actionPriority(a) - actionPriority(b) || b - a
    ))[0]
    visible.delete(drop)
  }

  const nextVisible = [...fixedIndexes, ...visible].sort((a, b) => a - b)
  const nextOverflow = candidates.filter(index => !visible.has(index))
  visibleActionIndexes.value = nextVisible
  overflowActionIndexes.value = nextOverflow
  if (!nextOverflow.length) menuOpen.value = false
}

function moreMenuItems(): HTMLButtonElement[] {
  if (!menuRef.value) return []
  return Array.from(menuRef.value.querySelectorAll<HTMLButtonElement>('.plugin-more-item:not([disabled])'))
}

async function openMore(): Promise<void> {
  if (menuOpen.value || !overflowActionIndexes.value.length) return
  menuOpen.value = true
  await nextTick()
  const items = moreMenuItems()
  if (items.length) items[0].focus()
  else menuRef.value?.focus()
}

function closeMore(returnFocus = true): void {
  if (!menuOpen.value) return
  menuOpen.value = false
  if (returnFocus) {
    void nextTick(() => {
      if (moreButtonRef.value?.isConnected) moreButtonRef.value.focus()
    })
  }
}

function toggleMore(): void {
  if (menuOpen.value) closeMore()
  else void openMore()
}

function onMoreKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.preventDefault()
    closeMore()
    return
  }
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp'
      && event.key !== 'Home' && event.key !== 'End') return
  const items = moreMenuItems()
  if (!items.length) return
  event.preventDefault()
  const current = items.indexOf(document.activeElement as HTMLButtonElement)
  let next = 0
  if (event.key === 'ArrowDown') next = current < 0 ? 0 : (current + 1) % items.length
  else if (event.key === 'ArrowUp') next = current <= 0 ? items.length - 1 : current - 1
  else if (event.key === 'End') next = items.length - 1
  items[next].focus()
}

function onMoreFocusOut(event: FocusEvent): void {
  const next = event.relatedTarget as Node | null
  if (!next) return
  if (moreRef.value?.contains(next)) return
  closeMore(false)
}

function onDocumentPointerDown(event: PointerEvent): void {
  const target = event.target as Node | null
  if (!target) return
  if (searchExpanded.value && !searchRef.value?.contains(target)) {
    searchExpanded.value = false
  }
  if (!menuOpen.value) return
  if (moreRef.value?.contains(target) || menuRef.value?.contains(target)) return
  closeMore(false)
}

function runOverflowAction(index: number): void {
  const action = trailingActions.value[index]
  if (!action || action.disabled) return
  closeMore()
  runAction(action)
}

watch(actions, () => {
  menuOpen.value = false
  scheduleMeasure()
}, { deep: true })

watch(overflowActionIndexes, (indexes) => {
  if (!indexes.length) menuOpen.value = false
})

watch([showSearch, () => props.nativeOpen, () => !!chrome.value.freshness], scheduleMeasure)

/** 扩展入口：native 交给壳的 NativePanel；view 型交给内容页（如网易云原生视图）。 */
function onOpenExtension(entry: ExtensionEntry): void {
  activeExtensionId.value = extensionKey(entry)
  if (entry.native && entry.native.url && entry.plugin) {
    emit('native', {
      plugin: entry.plugin,
      url: entry.native.url,
      title: String(entry.native.title || entry.label || entry.plugin),
    })
    return
  }
  if (entry.view) emit('view', entry.view)
}

function selectNav(id: string): void {
  activeExtensionId.value = ''
  emit('view', id)
}

watch(() => props.nativeOpen, (open) => {
  if (!open && activeExtensionId.value) activeExtensionId.value = ''
})
const freshness = ref<{ destroy?: () => void } | null>(null)
type FreshnessComponent = {
  mount?: (opts: Record<string, unknown>) => { destroy?: () => void }
}

/** 挂载统一「同步 / 校验」控件；挂载点会在 native 面板开/关时被重建。 */
function mountFreshness(): void {
  const spec = chrome.value.freshness
  const container = freshnessRef.value
  if (!spec || !container) return
  const Freshness = (window as unknown as { Freshness?: FreshnessComponent }).Freshness
  if (!Freshness || typeof Freshness.mount !== 'function') return
  const unit = typeof spec === 'object' && spec.unit ? spec.unit : '项'
  freshness.value = Freshness.mount({
    plugin: props.plugin.name,
    container,
    unit,
    onChange: () => emit('action', 'refresh'),
  }) || null
  // Freshness 挂载后才有关闭/校验按钮的真实宽度；首轮测量不能把它当成 0。
  scheduleMeasure()
}

function dropFreshness(): void {
  if (freshness.value && typeof freshness.value.destroy === 'function') freshness.value.destroy()
  freshness.value = null
}

// native 面板打开时顶栏整块换成面板动作（v-if / v-else），freshness 的挂载点随之
// 被删除；关闭面板后 Vue 只会建一个新的空 div。若只在 onMounted 挂一次，控件从此
// 消失（旧实例还挂在已移除的节点上继续轮询）。以 ref 变化为准重新挂载。
watch(freshnessRef, () => {
  dropFreshness()
  mountFreshness()
}, { flush: 'post' })

onMounted(() => {
  window.addEventListener('resize', scheduleMeasure)
  window.addEventListener('pointerdown', onDocumentPointerDown)
  if (toolbarRef.value && typeof ResizeObserver !== 'undefined') {
    toolbarObserver = new ResizeObserver(() => scheduleMeasure())
    toolbarObserver.observe(toolbarRef.value)
  }
  scheduleMeasure()
})

onBeforeUnmount(() => {
  stopQueryTimer()
  window.removeEventListener('resize', scheduleMeasure)
  window.removeEventListener('pointerdown', onDocumentPointerDown)
  if (toolbarObserver) {
    toolbarObserver.disconnect()
    toolbarObserver = null
  }
  if (measureTimer) window.clearTimeout(measureTimer)
  measureTimer = 0
  closeMore(false)
  dropFreshness()
})
</script>

<template>
  <div class="plugin-page" :style="pageStyle" :data-active-view="activeView">
    <aside
      class="plugin-chrome-sidebar"
      data-obx-splitter="--sub-sidebar-width"
      data-obx-splitter-min="180"
      data-obx-splitter-reserve="320"
      data-obx-splitter-default="240"
    >
      <div class="plugin-brand">
        <div class="plugin-brand-icon">
          <Icon :name="brand.icon || 'icon:package'" />
        </div>
        <div class="plugin-brand-text">
          <div class="plugin-brand-title">{{ brand.title || plugin.displayName }}</div>
          <div class="plugin-brand-sub">{{ brand.subtitle || '' }}</div>
        </div>
      </div>

      <nav class="plugin-nav obx-scroll">
        <template v-for="section in sidebarSections" :key="section.key">
          <template v-if="section.kind === 'nav'">
            <div v-if="section.group.section" class="plugin-nav-label">{{ section.group.section }}</div>
            <button
              v-for="item in section.group.items"
              :key="item.id"
              type="button"
              class="obx-nav-item plugin-nav-item"
              :class="{ active: activeView === item.id }"
              :data-view-id="item.id"
              @click="selectNav(item.id)"
            >
              <Icon v-if="item.icon" :name="item.icon" />
              <span class="plugin-nav-text">{{ item.label }}</span>
              <span v-if="badgeFor(item)" class="plugin-nav-count">{{ badgeFor(item) }}</span>
            </button>
          </template>
          <PluginExtensions
            v-else
            :plugin="plugin.name"
            :title="chrome.extensions?.title"
            :active-id="activeExtensionId"
            @open="onOpenExtension"
          />
        </template>
      </nav>

      <div v-if="showStatus" class="plugin-status" :data-status-kind="statusKind">
        <template v-if="statusItems.length">
          <span v-for="item in statusItems" :key="item.id" class="plugin-status-item">
            <span class="plugin-status-dot" :data-state="item.state || 'idle'"></span>
            <span class="plugin-status-text" :title="item.text">{{ item.text }}</span>
          </span>
        </template>
        <template v-else>
          <span v-if="statusEq" class="plugin-eq" aria-hidden="true">
            <i v-for="bar in 4" :key="bar" :style="{ '--plugin-eq-bar': bar }"></i>
          </span>
          <span v-else class="plugin-status-dot" :data-state="ui.status.state"></span>
          <span class="plugin-status-text" :title="ui.status.text">{{ ui.status.text }}</span>
        </template>
      </div>
    </aside>

    <div class="plugin-main">
      <div
        ref="toolbarRef"
        class="plugin-toolbar"
        :class="{
          'is-compact': toolbarCompact,
          'is-tight': toolbarTight,
          'is-ultra': toolbarUltra,
        }"
      >
        <div v-if="leadingActions.length" ref="leadingRef" class="plugin-leading">
          <button
            v-for="action in leadingActions"
            :key="'leading:' + action.id"
            type="button"
            class="btn"
            :class="actionClass(action)"
            :disabled="action.disabled"
            :title="action.label"
            :aria-label="action.label"
            @click="runAction(action)"
          >
            <Icon v-if="action.icon" :name="action.icon" />
            {{ action.label }}
          </button>
        </div>
        <div class="plugin-heading">
          <div class="plugin-title">{{ titleOverride || ui.title }}</div>
          <div class="plugin-sub" :title="titleOverride ? '' : ui.subtitle">
            {{ titleOverride ? '' : ui.subtitle }}
          </div>
        </div>
        <div ref="actionsRef" class="plugin-actions">
          <template v-if="nativeOpen">
            <button
              v-for="action in (nativeActions || []).filter(item => item.visible !== false)"
              :key="action.id + ':' + action.label"
              type="button"
              class="btn"
              :class="{
                'btn-primary': action.variant === 'primary',
                'btn-danger': action.variant === 'danger',
                active: !!action.active,
              }"
              :disabled="action.disabled"
              @click="emit('native-action', { id: action.id, payload: action.payload })"
            >
              <Icon v-if="action.icon" :name="action.icon" />
              {{ action.label }}
            </button>
          </template>
          <template v-else>
            <div
              v-if="showSearch"
              ref="searchRef"
              class="plugin-search-wrap"
              :class="{ 'is-open': searchExpanded }"
            >
              <button
                type="button"
                class="btn plugin-search-toggle"
                aria-label="搜索"
                :aria-expanded="searchExpanded ? 'true' : 'false'"
                @click="toggleSearch"
              >
                <Icon name="icon:search" />
              </button>
              <div class="search-field plugin-search">
                <span class="search-field-icon"><Icon name="icon:search" /></span>
                <input
                  v-model="query"
                  type="text"
                  :placeholder="search.placeholder"
                  autocomplete="off"
                  @input="onQueryInput"
                  @keydown.esc.prevent="searchExpanded = false"
                >
              </div>
            </div>
            <div v-if="chrome.freshness" ref="freshnessRef" class="plugin-freshness"></div>
            <template v-for="(action, index) in trailingActions" :key="action.id + ':' + index">
              <span
                v-if="action.kind === 'text'"
                :data-action-index="index"
                class="plugin-action-text"
              >{{ action.label }}</span>
              <span
                v-else-if="action.kind === 'badge'"
                :data-action-index="index"
                class="plugin-action-badge"
              >{{ action.label }}</span>
              <button
                v-else-if="visibleActionIndexes.includes(index)"
                :data-action-index="index"
                type="button"
                class="btn"
                :class="actionClass(action)"
                :disabled="action.disabled"
                :title="action.label"
                :aria-label="action.label"
                @click="runAction(action)"
              >
                <Icon v-if="action.icon" :name="action.icon" />
                {{ action.label }}
              </button>
            </template>
            <div
              v-if="overflowActionIndexes.length"
              ref="moreRef"
              class="plugin-more"
              @focusout="onMoreFocusOut"
            >
              <button
                ref="moreButtonRef"
                type="button"
                class="btn plugin-more-btn"
                aria-haspopup="menu"
                :aria-expanded="menuOpen ? 'true' : 'false'"
                aria-label="更多操作"
                @click="toggleMore"
                @keydown.esc.prevent.stop="closeMore()"
              >
                <span class="plugin-more-label">更多</span>
                <Icon name="icon:chevron-down" />
              </button>
              <div
                v-if="menuOpen"
                ref="menuRef"
                class="plugin-more-menu"
                role="menu"
                tabindex="-1"
                @keydown="onMoreKeydown"
              >
                <button
                  v-for="actionIndex in overflowActionIndexes"
                  :key="'more:' + actionIndex"
                  type="button"
                  class="btn plugin-more-item"
                  :class="actionClass(trailingActions[actionIndex])"
                  role="menuitem"
                  :disabled="trailingActions[actionIndex].disabled"
                  :aria-pressed="trailingActions[actionIndex].active ? 'true' : undefined"
                  @click="runOverflowAction(actionIndex)"
                >
                  <Icon v-if="trailingActions[actionIndex].icon" :name="trailingActions[actionIndex].icon" />
                  {{ trailingActions[actionIndex].label }}
                </button>
              </div>
            </div>
          </template>
        </div>
      </div>
      <div
        ref="measureRef"
        class="plugin-actions plugin-actions-measure"
        :class="{
          'is-compact': toolbarCompact,
          'is-tight': toolbarTight,
          'is-ultra': toolbarUltra,
        }"
        aria-hidden="true"
      >
        <template v-for="item in measureCandidates" :key="'measure:' + item.index">
          <button
            type="button"
            class="btn"
            :data-measure-action="item.index"
            tabindex="-1"
          >
            <Icon v-if="item.action.icon" :name="item.action.icon" />
            {{ item.action.label }}
          </button>
        </template>
        <button
          ref="moreMeasureRef"
          type="button"
          class="btn plugin-more-btn"
          tabindex="-1"
          aria-hidden="true"
        >
          <span class="plugin-more-label">更多</span>
          <Icon name="icon:chevron-down" />
        </button>
      </div>
      <div class="plugin-content">
        <slot />
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 迷你 EQ：媒体播放器在 `status.kind = eq-text` 且播放中（state=ok）时用四柱动画
   代替状态点。四根柱子的高度与相位各不相同，看起来才像在跳；柱子颜色跟随成功态，
   与 `state=ok` 的绿点保持同一套语义。 */
.plugin-eq {
  display: inline-flex;
  align-items: flex-end;
  gap: 2px;
  width: 18px;
  height: 14px;
  flex: 0 0 auto;
}
.plugin-eq i {
  width: 3px;
  height: 100%;
  border-radius: 1px;
  background: var(--success);
  transform-origin: bottom;
  animation: plugin-eq-bounce 0.9s ease-in-out infinite;
  animation-delay: calc(var(--plugin-eq-bar) * -0.18s);
}
.plugin-eq i:nth-child(1) { height: 45%; }
.plugin-eq i:nth-child(2) { height: 90%; }
.plugin-eq i:nth-child(3) { height: 65%; }
.plugin-eq i:nth-child(4) { height: 80%; }
@keyframes plugin-eq-bounce {
  0%, 100% { transform: scaleY(0.35); }
  50% { transform: scaleY(1); }
}
@media (prefers-reduced-motion: reduce) {
  .plugin-eq i { animation: none; transform: scaleY(0.6); }
}
</style>