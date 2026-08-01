<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const entryUrl = computed(() => route.meta.entryUrl as string)
</script>

<template>
  <div class="plugin-frame-container">
    <iframe :src="entryUrl" frameborder="0" class="plugin-iframe" allow="fullscreen *"></iframe>
  </div>
</template>

<style scoped>
.plugin-frame-container {width: 100%;height: 100%;border: none;outline: none;}
.plugin-iframe {width: 100%;height: 100%;border: 0;outline: none;display: block;}
</style>