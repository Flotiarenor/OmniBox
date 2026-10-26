<template>
  <div ref="host" class="settings-directory-host"></div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

interface FolderPickerList {
  element: HTMLElement
  getPaths: () => string[]
  setPaths: (paths: string[]) => void
}

interface FolderPickerApi {
  createList: (opts: Record<string, unknown>) => FolderPickerList
}

const props = withDefaults(defineProps<{
  paths: string[]
  single?: boolean
  placeholder?: string
  emptyText?: string
  localOnly?: boolean
  mount?: HTMLElement | null
}>(), {
  single: false,
  placeholder: '输入目录绝对路径',
  emptyText: '未添加任何目录',
  localOnly: false,
  mount: null,
})

const emit = defineEmits<{
  (event: 'update:paths', paths: string[]): void
}>()

const host = ref<HTMLElement | null>(null)
let list: FolderPickerList | null = null

function samePaths(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((path, index) => path === right[index])
}

onMounted(() => {
  if (!host.value) return
  const picker = (window as unknown as { FolderPicker?: FolderPickerApi }).FolderPicker
  if (!picker || typeof picker.createList !== 'function') return
  list = picker.createList({
    paths: props.paths.slice(),
    single: props.single,
    placeholder: props.placeholder,
    emptyText: props.emptyText,
    localOnly: props.localOnly,
    mount: props.mount || undefined,
    onChange: (paths: string[]) => emit('update:paths', paths.slice()),
  })
  host.value.appendChild(list.element)
})

watch(() => props.paths, (paths) => {
  if (!list) return
  const next = paths.slice()
  if (samePaths(list.getPaths(), next)) return
  list.setPaths(next)
}, { deep: true })

onBeforeUnmount(() => {
  list?.element.remove()
  list = null
})
</script>

<style scoped>
.settings-directory-host {
  width: 100%;
  min-width: 0;
}
</style>
