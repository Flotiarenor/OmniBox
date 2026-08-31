// shell/frontend/src/router/index.ts
import { createRouter as createVueRouter, createWebHistory } from 'vue-router'
import SettingsView from '../views/SettingsView.vue'
import StatusView from '../views/StatusView.vue'

export function createRouter() {
  return createVueRouter({
    history: createWebHistory(),
    routes: [
      {
        path: '/',
        component: { template: '<div class="loading">正在加载插件...</div>' }
      },
      {
        path: '/settings',
        name: 'settings',
        component: SettingsView
      },
      {
        path: '/status',
        name: 'status',
        component: StatusView
      }
    ]
  })
}