<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const entryUrl = computed(() => route.meta.entryUrl as string)
</script>

<template>
  <div class="plugin-frame-container">
    <iframe :src="entryUrl" frameborder="0" class="plugin-iframe"></iframe>
  </div>
</template>

<style scoped>
.plugin-frame-container, .plugin-iframe {
  width: 100%;
  height: 100%;
  border: none;
}
</style>