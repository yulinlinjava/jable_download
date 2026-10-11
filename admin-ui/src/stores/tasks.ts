import { defineStore } from 'pinia'
import { api, type CollectionItem, type CollectionTask, type SchedulerSettings } from '../api'

export const useTaskStore = defineStore('tasks', {
  state: () => ({
    collections: [] as CollectionTask[], resources: [] as CollectionItem[],
    collectionTotal: 0, collectionRunning: 0, resourceTotal: 0,
    settings: { enabled: true, concurrency: 1, segment_concurrency: 3, running: 0, scheduled: 0, completed: 0, failed: 0, total: 0, download_speed: 0, upload_speed: 0, video_upload_speed: 0, other_upload_speed: 0, updated_at: 0 } as SchedulerSettings,
    collectionQuery: { page: 1, page_size: 20, keyword: '', status: '', source: '' },
    resourceQuery: { page: 1, page_size: 20, title: '', source: '', status: '' },
    loading: false, collectionLoading: false, resourceLoading: false, settingsLoading: false,
    collectionRequestId: 0, resourceRequestId: 0, settingsRequestId: 0,
  }),
  getters: {
    activeDownloads: (state) => state.settings.scheduled,
    completedDownloads: (state) => state.settings.completed,
  },
  actions: {
    updateLoading() {
      this.loading = this.collectionLoading || this.resourceLoading || this.settingsLoading
    },
    async refreshCollections(silent = false) {
      const requestId = ++this.collectionRequestId
      const params = Object.fromEntries(Object.entries(this.collectionQuery).filter(([, value]) => value !== ''))
      if (!silent) { this.collectionLoading = true; this.updateLoading() }
      try {
        const response = await api.get('/collections', { params })
        if (requestId !== this.collectionRequestId) return false
        this.collections = response.data.data.items
        this.collectionTotal = response.data.data.total
        this.collectionRunning = response.data.data.running
        return true
      } finally {
        if (requestId === this.collectionRequestId) {
          this.collectionLoading = false
          this.updateLoading()
        }
      }
    },
    async refreshResources(silent = false) {
      const requestId = ++this.resourceRequestId
      const params = Object.fromEntries(Object.entries(this.resourceQuery).filter(([, value]) => value !== ''))
      if (!silent) { this.resourceLoading = true; this.updateLoading() }
      try {
        const response = await api.get('/collection-items', {
          params: { ...params, includeSyncState: false },
        })
        if (requestId !== this.resourceRequestId) return false
        this.resources = response.data.data.items
        this.resourceTotal = response.data.data.total
        return true
      } finally {
        if (requestId === this.resourceRequestId) {
          this.resourceLoading = false
          this.updateLoading()
        }
      }
    },
    async refreshSettings(silent = true) {
      const requestId = ++this.settingsRequestId
      if (!silent) { this.settingsLoading = true; this.updateLoading() }
      try {
        const response = await api.get('/settings/scheduler')
        if (requestId !== this.settingsRequestId) return false
        this.settings = response.data.data
        return true
      } finally {
        if (requestId === this.settingsRequestId) {
          this.settingsLoading = false
          this.updateLoading()
        }
      }
    },
    async refresh(silent = false) {
      await Promise.all([
        this.refreshCollections(silent),
        this.refreshResources(silent),
        this.refreshSettings(true),
      ])
    },
  },
})
