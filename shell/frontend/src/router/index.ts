// shell/frontend/src/router/index.ts
import { createRouter as createVueRouter, createWebHistory } from 'vue-router'

export function createRouter() {
  return createVueRouter({
    history: createWebHistory(),
    routes: [
      {
        path: '/',
        component: { template: '<div class="loading">正在加载插件...</div>' }
      }
    ]
  })
}