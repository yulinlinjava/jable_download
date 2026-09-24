import axios from 'axios'

export const api = axios.create({ baseURL: '/api', timeout: 45000 })
export const uploadApi = axios.create({ baseURL: '/', timeout: 0 })
const authorize = (config: any) => {
  const key = localStorage.getItem('jable-api-key')
  const target = new URL(config.url || '', window.location.origin)
  if (key && target.origin === window.location.origin) config.headers.set('X-API-Key', key)
  return config
}
const reject = (error: any) => {
  if (error.response?.status === 401) window.dispatchEvent(new CustomEvent('jable-auth-required'))
  const message = error.response?.data?.message || error.message || '请求失败'
  return Promise.reject(new Error(message))
}
api.interceptors.request.use(authorize)
uploadApi.interceptors.request.use(authorize)
api.interceptors.response.use((response) => response, reject)
uploadApi.interceptors.response.use((response) => response, reject)

export interface UploadResult {
  identifier?: string; path: string; fileName: string; size: number
  fileType: 'video' | 'image' | 'other'
  mediaType: string; previewType: 'video' | 'image' | 'audio' | 'pdf' | 'file'
  hlsPath?: string; coverPath?: string; posterPath?: string; previewPath?: string; vttPath?: string
  previewM3u8Path?: string; preview5M3u8Path?: string
  fallbackPath?: string; fallbackHlsPath?: string; fallbackCoverPath?: string
  fallbackPosterPath?: string; fallbackVttPath?: string
  segmented?: boolean; segmentDuration?: number; originalDeleted?: boolean
  processingStage?: string; warnings?: string[]; deduplicated?: boolean
  duration?: number; durationText?: string; width?: number; height?: number
  videoCodec?: string; audioCodec?: string; bitRate?: number
  originalSize?: number; processedSize?: number; compressed?: boolean; compressionRatio?: number
  r2SyncEnabled?: boolean; r2Synced?: boolean; r2SyncStatus?: 'unsynced' | 'syncing' | 'synced'
  r2SyncedAt?: number; localDeleted?: boolean; localDeletedAt?: number
}

export interface UploadRecord {
  identifier: string; fileName: string; fileType: 'video' | 'image' | 'other'
  mediaType: string; size: number; totalChunks?: number; uploadedCount: number
  completed: boolean; originalDeleted: boolean; status: string; processingStage?: string
  message?: string; error?: string; warnings: string[]; path?: string; hlsPath?: string
  coverPath?: string; posterPath?: string; previewPath?: string; vttPath?: string
  previewM3u8Path?: string; preview5M3u8Path?: string
  previewType: 'video' | 'image' | 'audio' | 'pdf' | 'file'
  segmented?: boolean; segmentDuration?: number; createdAt: number; updatedAt: number
  duration?: number; durationText?: string; width?: number; height?: number
  videoCodec?: string; audioCodec?: string; bitRate?: number
  originalSize?: number; processedSize?: number; compressed?: boolean; compressionRatio?: number
  r2SyncEnabled?: boolean; r2Synced?: boolean; r2SyncStatus?: 'unsynced' | 'syncing' | 'synced'
  r2SyncedAt?: number; localDeleted?: boolean; localDeletedAt?: number
}

export interface FfmpegTask {
  id: string; uploadIdentifier: string; fileName: string
  status: string; stage: string; progress: number
  compressionRequired?: boolean; compressionReason?: string
  message?: string; error?: string; duration?: number; durationText?: string
  width?: number; height?: number; videoCodec?: string; audioCodec?: string
  bitRate?: number; originalSize?: number; processedSize?: number
  compressed?: boolean; compressionRatio?: number
  path?: string; coverPath?: string
  createdAt: number; queuedAt: number; startedAt?: number; completedAt?: number; updatedAt: number
}

export interface FfmpegSettings {
  concurrency: number; maxConcurrency: number; paused: boolean
  running: number; queued: number; completed: number; failed: number; cancelled: number
}

export interface FfmpegCompressionSettings {
  enabled: boolean; minFileSizeMb: number; minDurationMinutes: number
  maxVideoBitrateMbps: number; maxWidth: number; maxHeight: number
  crf: number; preset: string; audioBitrateKbps: number
  hlsSegmentSeconds: number; presets: string[]
}

export interface R2Settings {
  enabled: boolean; endpointUrl: string; accessKeyId: string
  hasSecretAccessKey: boolean; bucket: string; publicBaseUrl: string
  pathPrefix: string; concurrency: number; maxConcurrency: number
  fileConcurrency: number; maxFileConcurrency: number
  autoUpload: boolean; autoCollection: boolean; updatedAt?: number
  cleanupEnabled: boolean; retentionDays: number
}

export interface R2SyncTask {
  id: string; resourceType: 'collection' | 'upload'; resourceId: string
  source?: string; title?: string; uploadIdentifier?: string; fileName: string
  fileType: 'video' | 'image' | 'other'; status: string
  totalFiles: number; syncedFiles: number; totalBytes: number; syncedBytes: number
  progress: number; objectKey?: string; publicUrl?: string
  message?: string; error?: string; createdAt: number
  startedAt?: number; completedAt?: number; updatedAt: number
}

export interface SystemMetrics {
  sampledAt: number; cpuPercent: number; cpuCores: number; cpuLimited: boolean
  memoryUsed: number; memoryLimit: number; memoryPercent: number; memoryLimited: boolean
  diskUsed: number; diskTotal: number; diskFree: number; diskPercent: number
  serviceCpuPercent: number; serviceMemory: number
  ffmpegCpuPercent: number; ffmpegMemory: number; ffmpegCount: number
  loadAverage1m: number; serviceUptime: number; sampleInterval: number; retentionDays: number
}

export interface SystemMetricPoint {
  sampledAt: number; cpuPercent: number; memoryPercent: number; diskPercent: number
  serviceCpuPercent: number; ffmpegCpuPercent: number; ffmpegMemory: number; ffmpegCount: number
}

export interface FfmpegProcess {
  pid: number; command: string; taskId?: string; fileName?: string; stage?: string
  progress?: number; cpuPercent: number; memoryUsed: number; startedAt?: number
  frame?: number; speed?: string; outTime?: string
}

export interface DownloadTask {
  task_id: string; source: string; video_id: string; url: string; title?: string
  r2_synced: boolean
  r2_sync_status: 'unsynced' | 'syncing' | 'synced'
  r2_synced_at?: number; local_deleted: boolean; local_deleted_at?: number
  status: string; downloaded_segments: number; total_segments: number
  downloaded_bytes: number; progress: number; poster_file?: string; cover_url?: string
  preview_file?: string; preview_url?: string; vtt_file?: string; vtt_url?: string; m3u8_url?: string
  preview_m3u8_file?: string; preview_m3u8_url?: string
  preview5_m3u8_file?: string; preview5_m3u8_url?: string
  fallback_m3u8_url?: string; fallback_vtt_url?: string; fallback_cover_url?: string
  chapters?: Array<{ index: number; title: string; duration?: string; status: string
    downloaded_segments: number; total_segments: number; progress: number
    m3u8_url?: string; vtt_url?: string; fallback_m3u8_url?: string; fallback_vtt_url?: string
    preview_m3u8_file?: string; preview_m3u8_url?: string
    preview5_m3u8_file?: string; preview5_m3u8_url?: string }>
  message?: string; error?: string; warnings: string[]; created_at: number; updated_at: number
}
export interface CollectionTask {
  id: string; name: string; url: string; batch_size: number; status: string
  scheduled_enabled: boolean; last_scheduled_date?: string
  r2_sync_enabled: boolean
  node_ids: string[]
  sync_nodes: Array<{ id: string; name: string; enabled: boolean; available: boolean }>
  last_total: number; last_created: number; last_skipped: number; last_task_ids: string[]
  last_node_queued: number; last_node_skipped: number; last_node_failed: number
  last_error?: string; last_run_at?: number; created_at: number; updated_at: number
}
export interface CollectionItem {
  id: string; collection_id?: string; source: string; video_id: string; title?: string
  url: string; cover?: string; preview_image?: string; preview_m3u8?: string
  preview5_m3u8?: string; duration?: string
  kind: 'collection' | 'download'; status: string; r2_synced: boolean
  r2_sync_status: 'unsynced' | 'syncing' | 'synced'; download_task_id?: string
  r2_synced_at?: number; local_deleted: boolean; local_deleted_at?: number
  synced_nodes: Array<{ node_id: string; node_name: string
    status: 'queued' | 'sending' | 'completed' | 'failed'; sent_at?: number; message?: string }>
  download?: DownloadTask; first_seen: number; last_seen: number
}
export interface NodeQueueSummary {
  queued: number; processing: number; succeeded: number; failed: number
}
export interface SchedulerSettings {
  enabled: boolean; concurrency: number; segment_concurrency: number
  running: number; scheduled: number
  completed: number; failed: number; total: number
  download_speed: number; upload_speed: number; updated_at: number
}

export interface PublicUrlSettings {
  url: string; source: 'dynamic' | 'environment' | 'default'
  updated_at?: number; environment_url: string; default_url: string
}

export interface ImportScript {
  id: string; name: string; description: string; code: string
  created_at: number; updated_at: number
}

export interface DownloadSigner {
  id: string; name: string; source: string; enabled: boolean; code: string
  config: Record<string, unknown>; created_at: number; updated_at: number
}
