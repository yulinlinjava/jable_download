<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CirclePlus, Collection, Document, Download, Film, Key, Monitor, Odometer, Refresh, Search, Setting, Upload, UploadFilled, VideoPlay } from '@element-plus/icons-vue'
import { api, uploadApi, type CollectionItem, type CollectionTask, type DownloadTask, type SystemMetrics, type UploadRecord, type UploadResult } from '../api'
import { useTaskStore } from '../stores/tasks'
import TaskThumbnail from '../components/TaskThumbnail.vue'
import VideoPlayer from '../components/VideoPlayer.vue'
import UploadPreview from '../components/UploadPreview.vue'
import UploadDocs from '../components/UploadDocs.vue'
import ResourceImport from '../components/ResourceImport.vue'
import ResourceSync from '../components/ResourceSync.vue'
import DownloadNodes from '../components/DownloadNodes.vue'
import SendToNode from '../components/SendToNode.vue'
import SendRecords from '../components/SendRecords.vue'
import DownloadSigners from '../components/DownloadSigners.vue'
import FfmpegTasks from '../components/FfmpegTasks.vue'
import SystemMonitor from '../components/SystemMonitor.vue'
import R2SyncTasks from '../components/R2SyncTasks.vue'
import SystemSettings from '../components/SystemSettings.vue'

const store = useTaskStore()
const activeTab = ref('collection')
const importVisible = ref(false), syncVisible = ref(false), nodeSendVisible = ref(false)
const formVisible = ref(false), keyVisible = ref(false), settingsVisible = ref(false), playerVisible = ref(false), uploadVisible = ref(false), uploadPreviewVisible = ref(false)
const authReady = ref(false), authenticating = ref(false)
const playing = ref<DownloadTask>(), editing = ref<string>()
const selectedCollections = ref<CollectionTask[]>([]), selectedResources = ref<CollectionItem[]>([])
const selectedUploads = ref<UploadRecord[]>([]), uploadRecords = ref<UploadRecord[]>([])
const collectionTable = ref(), resourceTable = ref()
const uploadTable = ref(), uploadTotal = ref(0), uploadsLoading = ref(false)
const keyValue = ref(localStorage.getItem('jable-api-key') || '')
const form = reactive({ name: '', url: '', batch_size: 1, scheduled_enabled: false,
  r2_sync_enabled: false, node_ids: [] as string[], mode: 'single', start_page: 1, end_page: 10 })
const collectionNodes = ref<Array<{ id: string; name: string; enabled: boolean; available: boolean }>>([])
const r2Enabled = ref(false), r2PublicBase = ref(''), r2PathPrefix = ref('')
const uploadFile = ref<File>(), uploadResult = ref<UploadResult>(), uploadPickerKey = ref(0)
const uploadPreviewResult = ref<UploadResult>()
const uploadState = reactive({ running: false, progress: 0, message: '请选择文件' })
const uploadQuery = reactive({ page: 1, page_size: 20, keyword: '', file_type: '', status: '' })
const systemSummary = ref<SystemMetrics>()
const monitorExpanded = ref(false)
let timer: number | undefined
const runningCollections = computed(() => store.collectionRunning)
const formatTime = (value?: number) => value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'
const formatBytes = (bytes: number) => bytes ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : '0 MB'
const formatSpeed = (bytes: number) => {
  if (!bytes) return '0 B/s'
  const units = ['B/s', 'KB/s', 'MB/s', 'GB/s']
  let value = bytes, index = 0
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1 }
  return `${value >= 100 ? value.toFixed(0) : value >= 10 ? value.toFixed(1) : value.toFixed(2)} ${units[index]}`
}
const statusType = (value: string) => ({ completed: 'success', failed: 'danger', downloading: 'primary', queued: 'warning', running: 'primary' }[value] || 'info') as any
const statusText = (value: string) => ({ pending: '待下载', completed: '已完成', failed: '失败', cancelled: '已取消', downloading: '下载中', queued: '排队中', idle: '空闲', running: '采集中', deleting: '删除中', uploading: '上传中', merging: '合并中', probing: '读取视频信息', compressing: '压缩视频', segmenting: '视频切片', generating_cover: '生成封面', generating_preview: '生成悬浮预览', generating_vtt: '生成进度预览' }[value] || value)
const r2StatusText = (value?: string) => ({ unsynced: '未同步', syncing: '同步中', synced: '同步完成' }[value || 'unsynced'] || '未同步')
const r2StatusType = (value?: string) => ({ syncing: 'warning', synced: 'success' }[value || 'unsynced'] || 'info') as any
const nodeStatusText = (value: string) => ({ queued: '等待发送', sending: '发送中', completed: '已发送', failed: '失败' }[value] || value)
const nodeStatusType = (value: string) => ({ queued: 'info', sending: 'warning', completed: 'success', failed: 'danger' }[value] || 'info') as any
const nodeStatusTip = (node: CollectionItem['synced_nodes'][number]) =>
  `${node.node_name} · ${nodeStatusText(node.status)} · ${formatTime(node.sent_at)}${node.message ? ` · ${node.message}` : ''}`

async function loadSystemSummary() { try { systemSummary.value = (await api.get('/system/metrics')).data.data } catch {} }
async function loadR2Availability() {
  try {
    const settings = (await api.get('/r2/settings')).data.data
    r2Enabled.value = Boolean(settings.enabled)
    r2PublicBase.value = String(settings.publicBaseUrl || '').replace(/\/$/, '')
    r2PathPrefix.value = String(settings.pathPrefix || '').replace(/^\/+|\/+$/g, '')
  }
  catch { r2Enabled.value = false }
}
async function loadCollectionNodes(current: CollectionTask['sync_nodes'] = []) {
  try {
    const sites = (await api.get('/resource-sync/sites')).data.data
      .filter((item: any) => item.resolve_media)
      .map((item: any) => ({ id: item.id, name: item.name, enabled: Boolean(item.enabled), available: Boolean(item.enabled) }))
    const byId = new Map(sites.map((item: any) => [item.id, item]))
    for (const item of current) if (!byId.has(item.id)) byId.set(item.id, item)
    collectionNodes.value = Array.from(byId.values()).filter((item: any) => item.enabled || form.node_ids.includes(item.id)) as any
  } catch { collectionNodes.value = current }
}
async function settingsUpdated() {
  keyValue.value = localStorage.getItem('jable-api-key') || ''
  await Promise.all([refresh(true), loadR2Availability()])
  if (activeTab.value === 'uploads') await loadUploads(true)
}
function r2Asset(value?: string, synced = false) {
  if (!value || !synced || !r2PublicBase.value) return value
  try {
    const path = new URL(value, window.location.origin).pathname.replace(/^\/+/, '')
    return `${r2PublicBase.value}/${r2PathPrefix.value ? r2PathPrefix.value + '/' : ''}${path}`
  } catch { return value }
}
function r2Download(task: DownloadTask, synced: boolean) {
  if (!synced || !r2PublicBase.value) return task
  return {
    ...task,
    fallback_m3u8_url: task.m3u8_url, fallback_vtt_url: task.vtt_url,
    fallback_cover_url: task.cover_url,
    cover_url: r2Asset(task.cover_url, true), preview_url: r2Asset(task.preview_url, true),
    vtt_url: r2Asset(task.vtt_url, true), m3u8_url: r2Asset(task.m3u8_url, true),
    preview_m3u8_url: r2Asset(task.preview_m3u8_url, true),
    preview5_m3u8_url: r2Asset(task.preview5_m3u8_url, true),
    chapters: task.chapters?.map(chapter => ({ ...chapter,
      fallback_m3u8_url: chapter.m3u8_url, fallback_vtt_url: chapter.vtt_url,
      m3u8_url: r2Asset(chapter.m3u8_url, true), vtt_url: r2Asset(chapter.vtt_url, true),
      preview_m3u8_url: r2Asset(chapter.preview_m3u8_url, true),
      preview5_m3u8_url: r2Asset(chapter.preview5_m3u8_url, true),
    })),
  } as DownloadTask
}
async function refresh(silent = false) { try { await Promise.all([store.refresh(silent), loadSystemSummary()]); if (activeTab.value === 'uploads') await loadUploads(true) } catch (e: any) { if (!silent) ElMessage.error(e.message) } }
async function openCreate() { editing.value = undefined; Object.assign(form, { name: '', url: '', batch_size: 1, scheduled_enabled: false, r2_sync_enabled: false, node_ids: [], mode: 'single', start_page: 1, end_page: 10 }); await Promise.all([loadR2Availability(), loadCollectionNodes()]); formVisible.value = true }
async function openEdit(row: CollectionTask) { editing.value = row.id; Object.assign(form, { name: row.name, url: row.url, batch_size: row.batch_size, scheduled_enabled: row.scheduled_enabled, r2_sync_enabled: row.r2_sync_enabled, node_ids: [...(row.node_ids || [])], mode: 'single', start_page: 1, end_page: 10 }); await Promise.all([loadR2Availability(), loadCollectionNodes(row.sync_nodes || [])]); formVisible.value = true }
async function saveCollection() {
  if (!form.url.trim()) return ElMessage.warning('请输入列表链接')
  if (!editing.value && form.mode === 'batch') {
    if (!form.url.includes('${page}')) return ElMessage.warning('批量链接必须包含 ${page}')
    if (form.end_page < form.start_page) return ElMessage.warning('结束页不能小于起始页')
    if (form.end_page - form.start_page + 1 > 500) return ElMessage.warning('单次最多添加 500 页')
  }
  try {
    if (editing.value) {
      await api.put(`/collections/${editing.value}`, form)
      ElMessage.success('任务已更新')
    } else if (form.mode === 'batch') {
      const result = (await api.post('/collections/bulk-create', {
        name: form.name, url_template: form.url, start_page: form.start_page,
        end_page: form.end_page, scheduled_enabled: form.scheduled_enabled,
        r2_sync_enabled: form.r2_sync_enabled, node_ids: form.node_ids,
      })).data.data
      ElMessage.success(`批量添加完成：新增 ${result.created} 个，跳过 ${result.skipped} 个`)
    } else {
      await api.post('/collections', form)
      ElMessage.success('任务已创建')
    }
    formVisible.value = false; await refresh()
  } catch (e: any) { ElMessage.error(e.message) }
}
async function execute(row: CollectionTask) {
  try { await api.post(`/collections/${row.id}/execute`); ElMessage.success('已触发采集'); await refresh(true) }
  catch (e: any) { ElMessage.error(e.message) }
}
async function bulkExecuteCollections() {
  if (!selectedCollections.value.length) return ElMessage.warning('请先选择采集任务')
  try {
    const result = (await api.post('/collections/bulk-execute', { ids: selectedCollections.value.map(x => x.id) })).data.data
    ElMessage.success(`已触发 ${result.queued} 个，跳过 ${result.skipped} 个`); await refresh(true)
  } catch (e: any) { ElMessage.error(e.message) }
}
async function bulkRemoveCollections() {
  if (!selectedCollections.value.length) return ElMessage.warning('请先选择采集任务')
  const count = selectedCollections.value.length
  await ElMessageBox.confirm(`删除选中的 ${count} 个采集任务？已经采集到的资源会保留。`, '批量删除确认', { type: 'warning' })
  try {
    const result = (await api.post('/collections/bulk-delete', {
      ids: selectedCollections.value.map(item => item.id),
    })).data.data
    const message = `已删除 ${result.deleted} 个，执行中跳过 ${result.skipped} 个，不存在 ${result.missing} 个`
    result.skipped || result.missing ? ElMessage.warning(message) : ElMessage.success(message)
    selectedCollections.value = []
    collectionTable.value?.clearSelection()
    await refresh()
  } catch (e: any) { ElMessage.error(e.message) }
}
function searchCollections() {
  store.collectionQuery.page = 1
  selectedCollections.value = []
  collectionTable.value?.clearSelection()
  refresh()
}
function changeCollectionPage(page: number) { store.collectionQuery.page = page; refresh(true) }
function changeCollectionSize(size: number) { store.collectionQuery.page_size = size; store.collectionQuery.page = 1; refresh(true) }
async function removeCollection(row: CollectionTask) {
  await ElMessageBox.confirm(`删除采集任务“${row.name}”？`, '删除确认', { type: 'warning' })
  try { await api.delete(`/collections/${row.id}`); ElMessage.success('已删除'); await refresh() } catch (e: any) { ElMessage.error(e.message) }
}
async function deduplicateDownloads() {
  await ElMessageBox.confirm('将按视频 code（忽略大小写）保留状态最好、更新时间最新的一条，并删除其他重复任务及其本地资源。是否继续？', '下载任务去重', { type: 'warning' })
  try {
    const result = (await api.post('/downloads/deduplicate')).data.data
    result.removed ? ElMessage.success(`发现 ${result.groups} 组重复，已删除 ${result.removed} 条`) : ElMessage.info('没有发现重复下载任务')
    await refresh()
  } catch (e: any) { ElMessage.error(e.message) }
}
function play(row: DownloadTask) { if (row.status === 'completed') { playing.value = row; playerVisible.value = true } }
async function playResource(row: CollectionItem) {
  if (row.status !== 'completed' || !row.download_task_id) return
  try {
    const task = row.download || (await api.get(`/download/${row.download_task_id}`)).data.data
    play(r2Download(task, row.r2_synced))
  } catch (e: any) { ElMessage.error(e.message) }
}
async function scheduleResource(row: CollectionItem) {
  try {
    await api.post(`/collection-items/${row.id}/download`)
    ElMessage.success('已加入下载调度'); await refresh(true)
  }
  catch (e: any) { ElMessage.error(e.message) }
}
async function bulkScheduleResources() {
  if (!selectedResources.value.length) return ElMessage.warning('请先选择采集资源')
  try {
    const result = (await api.post('/collection-items/bulk-download', { ids: selectedResources.value.map(x => x.id) })).data.data
    ElMessage.success(`已加入 ${result.queued} 个，跳过 ${result.skipped} 个`); await refresh(true)
  } catch (e: any) { ElMessage.error(e.message) }
}
async function bulkSyncResourcesToR2() {
  if (!selectedResources.value.length) return ElMessage.warning('请先选择资源')
  const taskIds = selectedResources.value.flatMap(item => item.status === 'completed' && item.download_task_id ? [item.download_task_id] : [])
  const incomplete = selectedResources.value.length - taskIds.length
  if (!taskIds.length) return ElMessage.warning('选中的资源都还没有下载完成')
  try {
    const result = (await api.post('/r2/resources/sync', { ids: taskIds })).data.data
    const message = `已加入 ${result.queued.length} 个，复用 ${result.reused.length} 个，跳过 ${result.skipped.length + incomplete} 个`
    result.skipped.length || incomplete ? ElMessage.warning(message) : ElMessage.success(message)
  } catch (e: any) { ElMessage.error(e.message) }
}
async function bulkRemoveResources() {
  if (!selectedResources.value.length) return ElMessage.warning('请先选择资源')
  const count = selectedResources.value.length
  await ElMessageBox.confirm(`确定删除选中的 ${count} 个资源？本地文件和数据库记录都会删除，操作无法恢复。`, '批量删除确认', { type: 'warning' })
  try {
    const result = (await api.post('/collection-items/bulk-delete', {
      ids: selectedResources.value.map(item => item.id),
    })).data.data
    if (result.failed) ElMessage.warning(`已删除 ${result.deleted} 个，失败 ${result.failed} 个`)
    else ElMessage.success(`已删除 ${result.deleted} 个资源`)
    selectedResources.value = []
    resourceTable.value?.clearSelection()
    await refresh()
  } catch (e: any) { ElMessage.error(e.message) }
}
async function exportResources() {
  if (!selectedResources.value.length) return ElMessage.warning('请先选择需要导出的资源')
  try {
    const response = await api.post('/collection-items/export', {
      ids: selectedResources.value.map(item => item.id),
    }, { responseType: 'blob' })
    const disposition = String(response.headers['content-disposition'] || '')
    const filename = disposition.match(/filename="?([^";]+)"?/)?.[1] || 'resources.json'
    const url = URL.createObjectURL(response.data)
    const link = document.createElement('a')
    link.href = url; link.download = filename; link.click()
    URL.revokeObjectURL(url)
    ElMessage.success(`已导出 ${selectedResources.value.length} 条资源`)
  } catch (e: any) { ElMessage.error(e.message) }
}
async function downloadAllPending() {
  await ElMessageBox.confirm('将所有分页中的待下载资源加入下载任务队列。接口会立即返回，实际下载由全局调度开关和并发数控制。是否继续？', '全部下载', { type: 'warning' })
  try {
    const result = (await api.post('/collection-items/download-all')).data.data
    if (!result.total) ElMessage.info('当前没有待下载资源')
    else ElMessage.success(`已处理 ${result.total} 个：新加入 ${result.queued} 个，复用已有任务 ${result.reused} 个`)
    await refresh(true)
  } catch (e: any) { ElMessage.error(e.message) }
}
async function removeResource(row: CollectionItem) {
  if (!row.download_task_id) return
  await ElMessageBox.confirm(`删除 ${row.video_id} 的资源记录和全部本地文件？`, '删除确认', { type: 'warning' })
  try { await api.delete(`/download/${row.download_task_id}`); ElMessage.success('资源和数据库记录已删除'); await refresh() }
  catch (e: any) { ElMessage.error(e.message) }
}
function searchResources() { store.resourceQuery.page = 1; refresh() }
function changeResourcePage(page: number) { store.resourceQuery.page = page; refresh(true) }
function changeResourceSize(size: number) { store.resourceQuery.page_size = size; store.resourceQuery.page = 1; refresh(true) }
function startPolling() {
  if (timer) window.clearInterval(timer)
  timer = window.setInterval(() => refresh(true), 2500)
}
function requireAuth() {
  authReady.value = false
  keyVisible.value = true
  if (timer) { window.clearInterval(timer); timer = undefined }
}
async function saveKey() {
  const key = keyValue.value.trim()
  if (!key) return ElMessage.warning('请输入 X-API-Key')
  authenticating.value = true
  localStorage.setItem('jable-api-key', key)
  try {
    await api.get('/auth/check')
    authReady.value = true
    keyVisible.value = false
    await Promise.all([refresh(), loadR2Availability()])
    startPolling()
    ElMessage.success('API Key 验证成功')
  } catch (e: any) {
    requireAuth()
    ElMessage.error(e.message)
  } finally { authenticating.value = false }
}
async function verifyStoredKey() {
  if (!keyValue.value.trim()) return requireAuth()
  authenticating.value = true
  try {
    await api.get('/auth/check')
    authReady.value = true
    await Promise.all([refresh(), loadR2Availability()])
    startPolling()
  } catch { requireAuth() } finally { authenticating.value = false }
}
function closeKey(done: () => void) { if (authReady.value) done() }

async function loadUploads(silent = false) {
  if (!silent) uploadsLoading.value = true
  try {
    const response = await uploadApi.get('/upload/list', { params: uploadQuery })
    uploadRecords.value = response.data.data.items
    uploadTotal.value = response.data.data.total
  } catch (e: any) { if (!silent) ElMessage.error(e.message) }
  finally { if (!silent) uploadsLoading.value = false }
}
function searchUploads() { uploadQuery.page = 1; loadUploads() }
function changeUploadPage(page: number) { uploadQuery.page = page; loadUploads(true) }
function changeUploadSize(size: number) { uploadQuery.page_size = size; uploadQuery.page = 1; loadUploads(true) }
function changeTab() { if (activeTab.value === 'uploads') loadUploads() }
function previewUploaded(row: UploadRecord) {
  if (!row.completed || !row.path) return
  uploadPreviewResult.value = row.r2Synced ? ({ ...row,
    path: r2Asset(row.path, true), hlsPath: r2Asset(row.hlsPath, true),
    coverPath: r2Asset(row.coverPath, true), posterPath: r2Asset(row.posterPath, true),
    previewPath: r2Asset(row.previewPath, true), vttPath: r2Asset(row.vttPath, true),
    previewM3u8Path: r2Asset(row.previewM3u8Path, true),
    preview5M3u8Path: r2Asset(row.preview5M3u8Path, true),
    fallbackPath: row.path, fallbackHlsPath: row.hlsPath,
    fallbackCoverPath: row.coverPath, fallbackPosterPath: row.posterPath,
    fallbackVttPath: row.vttPath,
  } as UploadResult) : row as UploadResult
  uploadPreviewVisible.value = true
}
const uploadProcessing = (row: UploadRecord) => ['merging', 'probing', 'compressing', 'segmenting', 'generating_cover', 'generating_preview', 'generating_vtt'].includes(row.status)
async function removeUpload(row: UploadRecord) {
  await ElMessageBox.confirm(`删除上传文件“${row.fileName}”及其全部本地资源？`, '删除确认', { type: 'warning' })
  try {
    await uploadApi.delete(`/upload/${row.identifier}`)
    ElMessage.success('上传文件已删除'); await loadUploads()
  } catch (e: any) { ElMessage.error(e.message) }
}
async function bulkRemoveUploads() {
  if (!selectedUploads.value.length) return ElMessage.warning('请先选择上传文件')
  await ElMessageBox.confirm(`删除选中的 ${selectedUploads.value.length} 个上传文件及全部本地资源？`, '批量删除确认', { type: 'warning' })
  try {
    const result = (await uploadApi.post('/upload/bulk-delete', {
      identifiers: selectedUploads.value.map(item => item.identifier),
    })).data.data
    const message = `已删除 ${result.deleted.length} 个，跳过 ${result.missing.length} 个，失败 ${result.failed.length} 个`
    result.failed.length ? ElMessage.warning(message) : ElMessage.success(message)
    selectedUploads.value = []; uploadTable.value?.clearSelection(); await loadUploads()
  } catch (e: any) { ElMessage.error(e.message) }
}
async function bulkSyncUploadsToR2() {
  if (!selectedUploads.value.length) return ElMessage.warning('请先选择上传文件')
  try {
    const result = (await api.post('/r2/uploads/sync', {
      ids: selectedUploads.value.map(item => item.identifier),
    })).data.data
    const message = `已加入 ${result.queued.length} 个，复用 ${result.reused.length} 个，跳过 ${result.skipped.length} 个`
    result.skipped.length ? ElMessage.warning(message) : ElMessage.success(message)
  } catch (e: any) { ElMessage.error(e.message) }
}

function openUpload() {
  uploadFile.value = undefined; uploadResult.value = undefined; uploadPickerKey.value += 1
  Object.assign(uploadState, { running: false, progress: 0, message: '请选择文件' })
  uploadVisible.value = true
}
function selectUpload(file: any) {
  if (!file.raw) return
  const blocked = /\.(?:asp|aspx|ashx|asmx|c|cc|cgi|class|cpp|cs|go|h|hpp|groovy|java|jar|jsp|jspx|kt|lua|php\d?|phtml|pl|py|pyc|rb|rs|scala|sh|swift|war|css|html?|js|jsx|mjs|svg|tsx|vue|wasm|xml|xhtml|app|apk|bat|bin|cmd|com|cpl|deb|dll|dmg|exe|hta|iso|lnk|msi|ps1|reg|rpm|scr|sys|vbe|vbs|wsf|conf|config|env|htaccess|ini|pem|sql|ya?ml)(?:\.|$)/i
  if (blocked.test(file.name)) {
    uploadFile.value = undefined
    return ElMessage.error('不允许上传脚本、源码、网页或可执行文件')
  }
  uploadFile.value = file.raw as File; uploadResult.value = undefined
  Object.assign(uploadState, { progress: 0, message: `已选择 ${file.name}` })
}
function metadataIdentifier(file: File) {
  const bytes = new TextEncoder().encode(`${file.name}\n${file.size}\n${file.lastModified}`)
  let hash = 0xcbf29ce484222325n
  for (const byte of bytes) {
    hash ^= BigInt(byte)
    hash = BigInt.asUintN(64, hash * 0x100000001b3n)
  }
  return hash.toString(16).padStart(16, '0')
}
async function uploadIdentifier(file: File) {
  const subtle = globalThis.crypto?.subtle
  if (!subtle) return metadataIdentifier(file)
  const sampleSize = 16 * 1024
  const sizeBytes = new TextEncoder().encode(`${file.size}\n`)
  const samples = file.size <= sampleSize * 2
    ? [await file.arrayBuffer()]
    : await Promise.all([
        file.slice(0, sampleSize).arrayBuffer(),
        file.slice(file.size - sampleSize).arrayBuffer(),
      ])
  const length = sizeBytes.byteLength + samples.reduce((total, sample) => total + sample.byteLength, 0)
  const bytes = new Uint8Array(length)
  bytes.set(sizeBytes)
  let offset = sizeBytes.byteLength
  for (const sample of samples) {
    bytes.set(new Uint8Array(sample), offset)
    offset += sample.byteLength
  }
  const digest = await subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest).subarray(0, 8))
    .map(value => value.toString(16).padStart(2, '0')).join('')
}
const wait = (milliseconds: number) => new Promise(resolve => window.setTimeout(resolve, milliseconds))
async function waitForUploadMerge(identifier: string) {
  while (true) {
    await wait(1000)
    const status = (await uploadApi.get('/upload/chunk', { params: { identifier } })).data.data
    uploadState.message = status.message || '正在后台合并文件'
    if (['failed', 'cancelled'].includes(status.status)) throw new Error(status.error || status.message || '后台视频处理已停止')
    if (status.completed && status.status === 'completed' && status.path) return status as UploadResult
  }
}
async function startUpload() {
  const file = uploadFile.value
  if (!file) return ElMessage.warning('请先选择文件')
  const chunkSize = 10 * 1024 * 1024
  const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize))
  uploadState.running = true; uploadState.progress = 0
  try {
    const identifier = await uploadIdentifier(file)
    uploadState.message = '正在检查已上传分片'
    const status = (await uploadApi.get('/upload/chunk', { params: { identifier } })).data.data
    const uploaded = new Set<number>(status.uploadedChunks || [])
    if (status.completed && status.path) {
      uploadResult.value = status as UploadResult
      uploadState.progress = 100; uploadState.message = '上传完成'
      return
    }
    for (let number = 1; number <= totalChunks; number += 1) {
      if (uploaded.has(number)) {
        uploadState.progress = Math.round(number / totalChunks * 90)
        continue
      }
      uploadState.message = `正在上传分片 ${number} / ${totalChunks}`
      const start = (number - 1) * chunkSize
      const body = new FormData()
      body.append('identifier', identifier)
      body.append('chunkNumber', String(number))
      body.append('totalChunks', String(totalChunks))
      body.append('file', file.slice(start, Math.min(file.size, start + chunkSize)))
      await uploadApi.post('/upload/chunk', body, { onUploadProgress: event => {
        const current = event.total ? event.loaded / event.total : 0
        uploadState.progress = Math.min(90, Math.round(((number - 1 + current) / totalChunks) * 90))
      } })
    }
    uploadState.progress = 92
    uploadState.message = file.type.startsWith('video/') ? '正在合并并提交 FFmpeg 队列' : '正在合并文件'
    const response = await uploadApi.post('/upload/merge', { identifier, fileName: file.name })
    const queued = response.data.data
    uploadState.progress = 95
    uploadState.message = queued.message || '已提交后台合并'
    uploadResult.value = await waitForUploadMerge(identifier)
    uploadState.progress = 100; uploadState.message = '上传完成'
    ElMessage.success('文件上传完成')
    await loadUploads(true)
  } catch (error: any) {
    uploadState.message = error.message || '上传失败'
    ElMessage.error(uploadState.message)
  } finally { uploadState.running = false }
}
function closeUpload(done: () => void) {
  if (uploadState.running) return ElMessage.warning('文件正在上传或处理，请稍候')
  done()
}

onMounted(() => { window.addEventListener('jable-auth-required', requireAuth); verifyStoredKey() })
onBeforeUnmount(() => { window.removeEventListener('jable-auth-required', requireAuth); if (timer) clearInterval(timer) })
</script>

<template>
  <div v-if="authReady" class="shell">
    <header class="topbar">
      <div><div class="eyebrow">JABLE TV DOWNLOAD</div><h1>资源采集控制台</h1><p>管理列表采集、下载队列与本地视频。</p></div>
      <div class="header-actions">
        <el-button :icon="Setting" @click="settingsVisible=true">系统配置</el-button>
        <el-button :icon="Key" circle @click="settingsVisible=true"/>
        <el-button :icon="Refresh" circle @click="refresh()"/>
      </div>
    </header>
    <section class="stats">
      <div class="stat"><span class="stat-icon indigo"><Collection/></span><div><strong>{{ store.resourceTotal }}</strong><span>资源总数</span></div></div>
      <div class="stat"><span class="stat-icon amber"><Download/></span><div><strong>{{ store.activeDownloads }}</strong><span>等待 / 下载中</span></div></div>
      <div class="stat"><span class="stat-icon green"><Film/></span><div><strong>{{ store.completedDownloads }}</strong><span>已完成资源</span></div></div>
      <div class="stat"><span class="stat-icon blue"><Refresh/></span><div><strong>{{ runningCollections }}</strong><span>正在采集</span></div></div>
      <div class="stat"><span class="stat-icon violet"><Odometer/></span><div><strong>{{ formatSpeed(store.settings.download_speed) }}</strong><span>下载网速</span></div></div>
      <div class="stat"><span class="stat-icon blue"><Upload/></span><div><strong>{{ formatSpeed(store.settings.upload_speed) }}</strong><span>上传网速</span></div></div>
    </section>

    <el-card shadow="never" class="panel monitor-bar">
      <div class="monitor-bar-content">
        <div class="monitor-bar-title"><span class="stat-icon indigo"><Monitor/></span><div><strong>运行监控</strong><small>最近 3 天资源趋势与 FFmpeg 进程</small></div></div>
        <div class="monitor-bar-metrics">
          <span><small>CPU</small><strong>{{(systemSummary?.cpuPercent || 0).toFixed(0)}}%</strong></span>
          <span><small>内存</small><strong>{{(systemSummary?.memoryPercent || 0).toFixed(0)}}%</strong></span>
          <span><small>磁盘</small><strong>{{(systemSummary?.diskPercent || 0).toFixed(0)}}%</strong></span>
          <span><small>FFmpeg</small><strong>{{systemSummary?.ffmpegCount || 0}} 个</strong></span>
        </div>
        <el-button :type="monitorExpanded ? 'default' : 'primary'" plain @click="monitorExpanded=!monitorExpanded">{{monitorExpanded ? '收起监控' : '查看趋势'}}</el-button>
      </div>
    </el-card>
    <el-collapse-transition>
      <SystemMonitor v-show="monitorExpanded" :active="monitorExpanded" :show-overview="false" class="embedded-system-monitor"/>
    </el-collapse-transition>

    <el-tabs v-model="activeTab" class="main-tabs" @tab-change="changeTab">
      <el-tab-pane label="列表采集" name="collection">
        <el-card shadow="never" class="panel">
          <template #header><div class="panel-head"><div><h2>列表采集</h2><p>执行后保存当前列表页全部视频信息，不会等待资源下载。</p></div><div class="panel-actions"><el-button :disabled="!selectedCollections.length" @click="bulkExecuteCollections">批量执行（{{selectedCollections.length}}）</el-button><el-button type="primary" :icon="CirclePlus" @click="openCreate">新建采集任务</el-button></div></div></template>
          <div class="filters collection-filters">
            <el-input v-model="store.collectionQuery.keyword" clearable :prefix-icon="Search" placeholder="搜索任务名称或列表 URL" @keyup.enter="searchCollections" @clear="searchCollections"/>
            <el-select v-model="store.collectionQuery.source" clearable placeholder="全部来源" @change="searchCollections"><el-option label="Jable" value="jable"/><el-option label="123AV" value="123av"/><el-option label="其他" value="other"/></el-select>
            <el-select v-model="store.collectionQuery.status" clearable placeholder="全部状态" @change="searchCollections"><el-option label="空闲" value="idle"/><el-option label="排队中" value="queued"/><el-option label="采集中" value="running"/></el-select>
            <el-button type="danger" plain :disabled="!selectedCollections.length" @click="bulkRemoveCollections">批量删除（{{selectedCollections.length}}）</el-button>
            <el-button type="primary" :icon="Search" @click="searchCollections">搜索</el-button>
          </div>
          <el-table ref="collectionTable" :data="store.collections" row-key="id" v-loading="store.loading" empty-text="还没有采集任务" @selection-change="selectedCollections=$event">
            <el-table-column type="selection" width="48" reserve-selection/>
            <el-table-column label="任务" min-width="220"><template #default="{row}"><div class="primary-cell">{{row.name}}</div><div class="secondary-cell ellipsis">{{row.url}}</div></template></el-table-column>
            <el-table-column label="定时采集" width="105"><template #default="{row}"><el-tag :type="row.scheduled_enabled?'success':'info'" effect="plain">{{row.scheduled_enabled?'每天 18:00':'已关闭'}}</el-tag></template></el-table-column>
            <el-table-column label="同步 R2" width="100"><template #default="{row}"><el-tag :type="row.r2_sync_enabled?'success':'info'" effect="plain">{{row.r2_sync_enabled?'已开启':'已关闭'}}</el-tag></template></el-table-column>
            <el-table-column label="同步节点" min-width="180"><template #default="{row}"><div v-if="row.sync_nodes?.length" class="node-tags"><el-tag v-for="node in row.sync_nodes" :key="node.id" :type="node.available?'success':'danger'" effect="plain">{{node.name}}</el-tag></div><span v-else class="secondary-cell">未配置</span></template></el-table-column>
            <el-table-column label="状态" width="105"><template #default="{row}"><el-tag :type="statusType(row.status)" effect="light">{{statusText(row.status)}}</el-tag></template></el-table-column>
            <el-table-column label="上次结果" min-width="250"><template #default="{row}"><template v-if="row.last_run_at"><div>共 {{row.last_total}}，新增 {{row.last_created}}，更新 {{row.last_skipped}}</div><div v-if="row.node_ids?.length" class="secondary-cell">节点：排队 {{row.last_node_queued}}，跳过 {{row.last_node_skipped}}，失败 {{row.last_node_failed}}</div></template><span v-else>尚未执行</span><div v-if="row.last_error" class="error-text">{{row.last_error}}</div></template></el-table-column>
            <el-table-column label="上次执行" width="175"><template #default="{row}">{{formatTime(row.last_run_at)}}</template></el-table-column>
            <el-table-column label="操作" width="205" fixed="right"><template #default="{row}"><el-button type="primary" link :disabled="row.status!=='idle'" @click="execute(row)">立即执行</el-button><el-button link :disabled="row.status!=='idle'" @click="openEdit(row)">编辑</el-button><el-button type="danger" link :disabled="row.status!=='idle'" @click="removeCollection(row)">删除</el-button></template></el-table-column>
          </el-table>
          <div class="pagination"><el-pagination v-model:current-page="store.collectionQuery.page" v-model:page-size="store.collectionQuery.page_size" :page-sizes="[20,50,100]" layout="total, sizes, prev, pager, next, jumper" :total="store.collectionTotal" @current-change="changeCollectionPage" @size-change="changeCollectionSize"/></div>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="资源管理" name="resources">
        <el-card shadow="never" class="panel resources-panel">
          <template #header><div class="panel-head"><div><h2>资源管理</h2><p>采集信息、下载状态和本地资源合并展示，每个视频只显示一条。</p><div class="node-queue-summary"><el-tag type="warning" effect="plain">等待发送 {{store.nodeQueue.queued}}</el-tag><el-tag type="primary" effect="plain">处理中 {{store.nodeQueue.processing}}</el-tag><el-tag type="success" effect="plain">已发送 {{store.nodeQueue.succeeded}}</el-tag><el-tag v-if="store.nodeQueue.failed" type="danger" effect="plain">失败 {{store.nodeQueue.failed}}</el-tag></div></div><div class="panel-actions"><el-button type="primary" plain @click="importVisible=true">导入数据</el-button><el-button :disabled="!selectedResources.length" @click="exportResources">导出 JSON（{{selectedResources.length}}）</el-button><el-button type="success" plain @click="nodeSendVisible=true">调度至下载节点</el-button><el-button @click="syncVisible=true">发送数据</el-button><el-button @click="deduplicateDownloads">按番号去重</el-button><el-button type="success" plain :disabled="!selectedResources.length" @click="bulkSyncResourcesToR2">同步 R2（{{selectedResources.length}}）</el-button><el-button type="danger" plain :disabled="!selectedResources.length" @click="bulkRemoveResources">批量删除（{{selectedResources.length}}）</el-button><el-button type="primary" plain :disabled="!selectedResources.length" @click="bulkScheduleResources">批量下载（{{selectedResources.length}}）</el-button><el-button type="primary" @click="downloadAllPending">全部下载</el-button></div></div></template>
          <div class="filters">
            <el-input v-model="store.resourceQuery.title" clearable :prefix-icon="Search" placeholder="标题或番号模糊搜索" @keyup.enter="searchResources" @clear="searchResources"/>
            <el-select v-model="store.resourceQuery.source" filterable allow-create clearable placeholder="全部来源" @change="searchResources"><el-option label="Jable" value="jable"/><el-option label="123AV" value="123av"/><el-option label="外部导入" value="external"/></el-select>
            <el-select v-model="store.resourceQuery.status" clearable placeholder="全部状态" @change="searchResources"><el-option label="待下载" value="pending"/><el-option label="排队中" value="queued"/><el-option label="下载中" value="downloading"/><el-option label="已完成" value="completed"/><el-option label="失败" value="failed"/></el-select>
            <el-button type="primary" :icon="Search" @click="searchResources">搜索</el-button>
          </div>
          <el-table ref="resourceTable" :data="store.resources" row-key="id" empty-text="暂无资源" @selection-change="selectedResources=$event" @row-click="playResource" :row-class-name="({row}) => row.status==='completed' ? 'playable-row' : ''">
            <el-table-column type="selection" width="48" reserve-selection/>
            <el-table-column label="资源" min-width="500"><template #default="{row}"><div class="resource-cell"><TaskThumbnail :cover="r2Asset(row.download?.cover_url || row.cover, row.r2_synced)" :fallback-cover="row.download?.cover_url || row.cover" :preview="r2Asset(row.download?.preview_url || row.preview_image, row.r2_synced)" :fallback-preview="row.download?.preview_url || row.preview_image" @click.stop="playResource(row)"/><div class="resource-copy"><div class="primary-cell line-clamp">{{row.title || row.video_id}}</div><div class="secondary-cell">{{row.source}} · {{row.video_id}}<span v-if="row.duration"> · {{row.duration}}</span></div><div v-if="row.download?.error" class="error-text">{{row.download.error}}</div><div v-else-if="row.download?.warnings?.length" class="warning-text">{{row.download.warnings.join('；')}}</div><div v-else-if="row.download?.message" class="secondary-cell">{{row.download.message}}</div></div></div></template></el-table-column>
            <el-table-column label="状态" width="105"><template #default="{row}"><el-tag :type="statusType(row.status)">{{statusText(row.status)}}</el-tag></template></el-table-column>
            <el-table-column label="R2同步" width="120"><template #default="{row}"><div class="tag-stack"><el-tag :type="r2StatusType(row.r2_sync_status)" effect="plain">{{r2StatusText(row.r2_sync_status)}}</el-tag><el-tag v-if="row.local_deleted" type="info" effect="plain">本地已清理</el-tag></div></template></el-table-column>
            <el-table-column label="同步节点" min-width="210"><template #default="{row}"><div v-if="row.synced_nodes?.length" class="node-tags"><el-tooltip v-for="node in row.synced_nodes" :key="node.node_id" :content="nodeStatusTip(node)" placement="top"><el-tag :type="nodeStatusType(node.status)" effect="plain">{{node.node_name}} · {{nodeStatusText(node.status)}}</el-tag></el-tooltip></div><span v-else class="secondary-cell">未调度</span></template></el-table-column>
            <el-table-column label="下载进度" min-width="190"><template #default="{row}"><template v-if="row.download"><el-progress :percentage="Math.round(row.download.progress)" :status="row.status==='failed'?'exception':row.status==='completed'?'success':undefined"/><div class="secondary-cell">{{row.download.downloaded_segments}} / {{row.download.total_segments}} 分片 · {{formatBytes(row.download.downloaded_bytes)}}</div></template><span v-else class="secondary-cell">尚未加入下载队列</span></template></el-table-column>
            <el-table-column label="采集 / 更新" width="175"><template #default="{row}">{{formatTime(row.download?.updated_at || row.first_seen)}}</template></el-table-column>
            <el-table-column label="操作" width="165" fixed="right"><template #default="{row}"><el-button v-if="row.status==='completed'" type="primary" link :icon="VideoPlay" @click.stop="playResource(row)">播放</el-button><el-button v-else-if="['pending','failed'].includes(row.status)" type="primary" link @click.stop="scheduleResource(row)">{{row.status==='failed'?'重新下载':'加入队列'}}</el-button><el-button v-if="row.download_task_id" type="danger" link @click.stop="removeResource(row)">删除</el-button></template></el-table-column>
          </el-table>
          <div class="pagination"><el-pagination v-model:current-page="store.resourceQuery.page" v-model:page-size="store.resourceQuery.page_size" :page-sizes="[20,50,100]" layout="total, sizes, prev, pager, next, jumper" :total="store.resourceTotal" @current-change="changeResourcePage" @size-change="changeResourceSize"/></div>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="下载节点" name="download-nodes">
        <DownloadNodes/>
      </el-tab-pane>

      <el-tab-pane label="发送记录" name="send-records">
        <SendRecords/>
      </el-tab-pane>

      <el-tab-pane label="上传文件" name="uploads">
        <el-card shadow="never" class="panel">
          <template #header><div class="panel-head"><div><h2>上传文件</h2><p>查看通过接口和管理后台上传的文件、处理状态与本地资源。</p></div><div class="panel-actions"><el-button type="success" plain :disabled="!selectedUploads.length" @click="bulkSyncUploadsToR2">同步 R2（{{selectedUploads.length}}）</el-button><el-button type="danger" plain :disabled="!selectedUploads.length" @click="bulkRemoveUploads">批量删除（{{selectedUploads.length}}）</el-button><el-button type="primary" :icon="Upload" @click="openUpload">上传文件</el-button></div></div></template>
          <div class="filters upload-filters">
            <el-input v-model="uploadQuery.keyword" clearable :prefix-icon="Search" placeholder="搜索文件名、资源 ID 或 URL" @keyup.enter="searchUploads" @clear="searchUploads"/>
            <el-select v-model="uploadQuery.file_type" clearable placeholder="全部类型" @change="searchUploads"><el-option label="视频" value="video"/><el-option label="图片" value="image"/><el-option label="其他" value="other"/></el-select>
            <el-select v-model="uploadQuery.status" clearable placeholder="全部状态" @change="searchUploads"><el-option label="上传中" value="uploading"/><el-option label="合并中" value="merging"/><el-option label="等待 FFmpeg" value="queued"/><el-option label="读取视频信息" value="probing"/><el-option label="压缩视频" value="compressing"/><el-option label="视频切片" value="segmenting"/><el-option label="生成封面" value="generating_cover"/><el-option label="生成悬浮预览" value="generating_preview"/><el-option label="生成进度预览" value="generating_vtt"/><el-option label="已完成" value="completed"/><el-option label="失败" value="failed"/><el-option label="已取消" value="cancelled"/></el-select>
            <el-button type="primary" :icon="Search" @click="searchUploads">搜索</el-button>
          </div>
          <el-table ref="uploadTable" :data="uploadRecords" row-key="identifier" v-loading="uploadsLoading" empty-text="暂无上传文件" @selection-change="selectedUploads=$event" @row-click="previewUploaded" :row-class-name="({row}) => row.completed ? 'playable-row' : ''">
            <el-table-column type="selection" width="48" reserve-selection/>
            <el-table-column label="文件" min-width="500"><template #default="{row}"><div class="resource-cell"><TaskThumbnail v-if="row.fileType==='video'" :cover="row.completed ? r2Asset(row.coverPath, row.r2Synced) : undefined" :fallback-cover="row.coverPath" :preview="row.completed ? r2Asset(row.previewPath, row.r2Synced) : undefined" :fallback-preview="row.previewPath" @click.stop="previewUploaded(row)"/><TaskThumbnail v-else-if="row.fileType==='image'" :cover="row.completed ? r2Asset(row.path, row.r2Synced) : undefined" :fallback-cover="row.path" @click.stop="previewUploaded(row)"/><div v-else class="upload-file-icon" @click.stop="previewUploaded(row)"><Document/></div><div class="resource-copy"><div class="primary-cell line-clamp">{{row.fileName}}</div><div class="secondary-cell">{{row.identifier}} · {{row.fileType}} · {{formatBytes(row.originalSize || row.size)}}<template v-if="row.durationText"> · {{row.durationText}}</template><template v-if="row.width && row.height"> · {{row.width}}×{{row.height}}</template></div><div v-if="row.completed && row.fileType==='video'" class="secondary-cell"><template v-if="row.compressed">已压缩为 {{formatBytes(row.processedSize || 0)}} · 减少 {{row.compressionRatio || 0}}%</template><template v-else>无需压缩<template v-if="row.processedSize"> · HLS 分片 {{formatBytes(row.processedSize)}}</template></template></div><div v-if="row.error" class="error-text">{{row.error}}</div><div v-else-if="row.warnings?.length" class="warning-text">{{row.warnings.join('；')}}</div><div v-else class="secondary-cell">{{row.message}}</div></div></div></template></el-table-column>
            <el-table-column label="状态" width="145"><template #default="{row}"><el-tag :type="statusType(row.status)">{{statusText(row.status)}}</el-tag></template></el-table-column>
            <el-table-column label="R2同步" width="120"><template #default="{row}"><div class="tag-stack"><el-tag :type="r2StatusType(row.r2SyncStatus)" effect="plain">{{r2StatusText(row.r2SyncStatus)}}</el-tag><el-tag v-if="row.localDeleted" type="info" effect="plain">本地已清理</el-tag></div></template></el-table-column>
            <el-table-column label="分片" width="120"><template #default="{row}"><span v-if="row.totalChunks">{{row.uploadedCount}} / {{row.totalChunks}}</span><span v-else>—</span></template></el-table-column>
            <el-table-column label="上传 / 更新" width="175"><template #default="{row}">{{formatTime(row.updatedAt)}}</template></el-table-column>
            <el-table-column label="操作" width="150" fixed="right"><template #default="{row}"><el-button v-if="row.completed" type="primary" link @click.stop="previewUploaded(row)">预览</el-button><el-button type="danger" link :disabled="uploadProcessing(row)" @click.stop="removeUpload(row)">删除</el-button></template></el-table-column>
          </el-table>
          <div class="pagination"><el-pagination v-model:current-page="uploadQuery.page" v-model:page-size="uploadQuery.page_size" :page-sizes="[20,50,100]" layout="total, sizes, prev, pager, next, jumper" :total="uploadTotal" @current-change="changeUploadPage" @size-change="changeUploadSize"/></div>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="FFmpeg 任务" name="ffmpeg-tasks">
        <FfmpegTasks :active="activeTab==='ffmpeg-tasks'"/>
      </el-tab-pane>

      <el-tab-pane label="CF R2资源同步日志" name="r2-sync">
        <R2SyncTasks :active="activeTab==='r2-sync'"/>
      </el-tab-pane>

      <el-tab-pane label="签名脚本" name="download-signers">
        <DownloadSigners/>
      </el-tab-pane>

      <el-tab-pane label="分片上传文档" name="upload-docs">
        <UploadDocs/>
      </el-tab-pane>
    </el-tabs>
  </div>

  <SystemSettings v-if="authReady" v-model="settingsVisible" @updated="settingsUpdated"/>
  <ResourceImport v-if="authReady" v-model="importVisible" @imported="searchResources"/>
  <ResourceSync v-if="authReady" v-model="syncVisible" :ids="selectedResources.filter(item => item.status==='completed').map(item => item.id)" @synced="refresh(true)"/>
  <SendToNode v-if="authReady" v-model="nodeSendVisible" :ids="selectedResources.map(item => item.id)" @sent="refresh(true)"/>
  <el-dialog v-model="formVisible" :title="editing ? '编辑采集任务' : '新建采集任务'" width="620px">
    <el-form label-position="top">
      <el-form-item v-if="!editing" label="添加方式"><el-radio-group v-model="form.mode"><el-radio-button value="single">单条添加</el-radio-button><el-radio-button value="batch">批量添加</el-radio-button></el-radio-group></el-form-item>
      <el-form-item label="任务名称"><el-input v-model="form.name" :placeholder="form.mode==='batch' ? '例如：123AV FC2，生成后自动附加页码' : '可选，例如：123AV 今日 FC2'"/></el-form-item>
      <el-form-item :label="form.mode==='batch' ? '列表链接模板' : '列表链接'" required><el-input v-model="form.url" :placeholder="form.mode==='batch' ? 'https://123av.com/tw/makers/fc2?page=${page}' : 'https://123av.com/cn/makers/fc2?sort=today&page=1'"/><div v-if="form.mode==='batch'" class="form-tip">使用 ${page} 作为页码变量，其他路径和查询参数保持不变。</div></el-form-item>
      <el-form-item v-if="!editing && form.mode==='batch'" label="页码范围"><div class="page-range"><el-input-number v-model="form.start_page" :min="1" :max="100000"/><span>至</span><el-input-number v-model="form.end_page" :min="form.start_page" :max="Math.min(100000, form.start_page + 499)"/><span>共 {{Math.max(0, form.end_page-form.start_page+1)}} 个任务</span></div></el-form-item>
      <el-form-item label="每天定时采集"><div class="schedule-field"><el-switch v-model="form.scheduled_enabled"/><span>开启后每天北京时间 18:00 自动执行</span></div></el-form-item>
      <el-form-item label="同步节点"><el-select v-model="form.node_ids" multiple filterable collapse-tags collapse-tags-tooltip clearable placeholder="可选择多个下载节点" style="width:100%"><el-option v-for="node in collectionNodes" :key="node.id" :label="node.available ? node.name : `${node.name}（不可用）`" :value="node.id" :disabled="!node.available"/></el-select><div class="form-tip">每采集成功一条资源，就会分别加入所选节点的异步发送队列。</div></el-form-item>
      <el-form-item label="同步到 Cloudflare R2"><div class="schedule-field"><el-switch v-model="form.r2_sync_enabled" :disabled="!r2Enabled && !form.r2_sync_enabled"/><span>{{r2Enabled ? '资源下载完成后自动加入 R2 同步队列' : '请先在系统配置中配置并启用 R2'}}</span></div></el-form-item>
    </el-form>
    <template #footer><el-button @click="formVisible=false">取消</el-button><el-button type="primary" @click="saveCollection">保存</el-button></template>
  </el-dialog>
  <el-dialog v-model="keyVisible" title="API 访问验证" width="440px" :show-close="authReady" :close-on-click-modal="false" :close-on-press-escape="authReady" :before-close="closeKey">
    <div class="auth-note">请输入服务端配置的 X-API-Key，通过验证后才能进入管理系统。</div>
    <el-input v-model="keyValue" type="password" show-password autocomplete="current-password" placeholder="请输入 X-API-Key" @keyup.enter="saveKey"/>
    <template #footer><el-button v-if="authReady" @click="keyVisible=false">取消</el-button><el-button type="primary" :loading="authenticating" @click="saveKey">验证并进入</el-button></template>
  </el-dialog>
  <el-dialog v-model="playerVisible" :title="playing?.title || playing?.video_id || '视频播放'" width="min(1000px, 92vw)" destroy-on-close class="video-dialog"><VideoPlayer v-if="playing && playerVisible" :task="playing"/></el-dialog>
  <el-dialog v-model="uploadVisible" title="上传文件" width="min(820px, 92vw)" :close-on-click-modal="false" :before-close="closeUpload" destroy-on-close>
    <template v-if="!uploadResult">
      <el-upload :key="uploadPickerKey" drag :auto-upload="false" :limit="1" :disabled="uploadState.running" :on-change="selectUpload" action="#">
        <el-icon class="el-icon--upload"><UploadFilled/></el-icon><div class="el-upload__text">拖入文件，或 <em>点击选择</em></div><template #tip><div class="el-upload__tip">文件按 10 MB 分片上传；视频合并后自动生成 3 秒 HLS 切片。禁止上传脚本、源码、网页和可执行文件。</div></template>
      </el-upload>
      <div v-if="uploadFile" class="upload-progress"><div><strong>{{uploadState.message}}</strong><span>{{formatBytes(uploadFile.size)}}</span></div><el-progress :percentage="uploadState.progress" :status="uploadState.progress===100?'success':undefined"/></div>
    </template>
    <UploadPreview v-else :key="uploadResult.path" :result="uploadResult"/>
    <template #footer><el-button :disabled="uploadState.running" @click="uploadVisible=false">{{uploadResult?'关闭':'取消'}}</el-button><el-button v-if="!uploadResult" type="primary" :loading="uploadState.running" :disabled="!uploadFile" @click="startUpload">开始上传</el-button></template>
  </el-dialog>
  <el-dialog v-model="uploadPreviewVisible" :title="uploadPreviewResult?.fileName || '文件预览'" width="min(1000px, 92vw)" destroy-on-close class="video-dialog"><UploadPreview v-if="uploadPreviewResult && uploadPreviewVisible" :result="uploadPreviewResult"/></el-dialog>
</template>
