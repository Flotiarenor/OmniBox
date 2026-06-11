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
.plugin-frame-container {
  width: 100%;
  height: 100%;
  border: none;
  outline: none;
}

.plugin-iframe {
  width: 100%;
  height: 100%;
  border: 0;                  /* 移除默认边框 */
  outline: none;              /* 移除聚焦轮廓 */
  display: block;             /* 消除 iframe 底部空隙 */
}
</style>