import { createRouter as createVueRouter, createWebHistory } from 'vue-router'

export function createRouter() {
  return createVueRouter({
    history: createWebHistory(),
    routes: [
      // 初始只有根路由，插件路由动态注入
      { path: '/', component: () => import('../App.vue') }
    ]
  })
}