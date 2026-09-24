import { createRouter, createWebHistory } from 'vue-router'
import Dashboard from './views/Dashboard.vue'

export default createRouter({
  history: createWebHistory('/admin/'),
  routes: [{ path: '/', component: Dashboard }],
})
