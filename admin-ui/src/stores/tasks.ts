import { defineStore } from 'pinia'
import { api, type CollectionItem, type CollectionTask, type NodeQueueSummary, type SchedulerSettings } from '../api'

export const useTaskStore = defineStore('tasks', {
  state: () => ({
    collections: [] as CollectionTask[], resources: [] as CollectionItem[],
    collectionTotal: 0, collectionRunning: 0, resourceTotal: 0,
    nodeQueue: { queued: 0, processing: 0, succeeded: 0, failed: 0 } as NodeQueueSummary,
    settings: { enabled: true, concurrency: 1, segment_concurrency: 3, running: 0, scheduled: 0, completed: 0, failed: 0, total: 0, download_speed: 0, upload_speed: 0, updated_at: 0 } as SchedulerSettings,
    collectionQuery: { page: 1, page_size: 20, keyword: '', status: '', source: '' },
    resourceQuery: { page: 1, page_size: 20, title: '', source: '', status: '' }, loading: false,
  }),
  getters: {
    activeDownloads: (state) => state.settings.scheduled,
    completedDownloads: (state) => state.settings.completed,
  },
  actions: {
    async refresh(silent = false) {
      if (!silent) this.loading = true
      try {
        const collectionParams = Object.fromEntries(Object.entries(this.collectionQuery).filter(([, value]) => value !== ''))
        const resourceParams = Object.fromEntries(Object.entries(this.resourceQuery).filter(([, value]) => value !== ''))
        const [collections, resources, settings] = await Promise.all([
          api.get('/collections', { params: collectionParams }),
          api.get('/collection-items', { params: resourceParams }), api.get('/settings/scheduler'),
        ])
        this.collections = collections.data.data.items
        this.collectionTotal = collections.data.data.total
        this.collectionRunning = collections.data.data.running
        this.resources = resources.data.data.items
        this.resourceTotal = resources.data.data.total
        this.nodeQueue = resources.data.data.node_queue || this.nodeQueue
        this.settings = settings.data.data
      } finally { this.loading = false }
    },
  },
})
