<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Refresh, RefreshLeft, Search } from '@element-plus/icons-vue'
import { api, type R2Settings, type R2SyncTask } from '../api'

const props = defineProps<{ active: boolean }>()
const loading = ref(false), total = ref(0), tasks = ref<R2SyncTask[]>([])
const selected = ref<R2SyncTask[]>([]), table = ref()
const settings = ref<R2Settings>({
  enabled: false, endpointUrl: '', accessKeyId: '', hasSecretAccessKey: false,
  bucket: '', publicBaseUrl: '', pathPrefix: '', maxConcurrency: 16,
  maxFileConcurrency: 16, videoConcurrency: 1, videoFileConcurrency: 3,
  otherConcurrency: 3, otherFileConcurrency: 3,
  autoUpload: false, autoCollection: false, cleanupEnabled: false, retentionDays: 7,
})
const query = reactive({ page: 1, page_size: 20, keyword: '', status: '', resource_type: '', sync_group: '' })
const counts = reactive<Record<string, number>>({ queued: 0, running: 0, completed: 0, failed: 0 })
const groupCounts = reactive<Record<'video' | 'other', Record<string, number>>>({
  video: { queued: 0, running: 0, completed: 0, failed: 0 },
  other: { queued: 0, running: 0, completed: 0, failed: 0 },
})
let timer: number | undefined
let loadPromise: Promise<void> | undefined
let pollingGeneration = 0

const retryable = computed(() => selected.value.filter(item => item.status === 'failed'))
const deletable = computed(() => selected.value.filter(item => item.status !== 'running'))
const formatBytes = (value?: number) => {
  if (!value) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']; let size = value, index = 0
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1 }
  return `${size >= 100 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`
}
const formatTime = (value?: number) => value
  ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'
const statusText = (value: string, nextRetryAt?: number) => nextRetryAt && value === 'queued'
  ? '等待重试' : ({
  queued: '等待同步', running: '同步中', completed: '已完成', failed: '失败',
}[value] || value)
const statusType = (value: string) => ({
  completed: 'success', failed: 'danger', queued: 'warning', running: 'primary',
}[value] || 'info') as any

async function load(silent = false) {
  if (loadPromise) return loadPromise
  const request = (async () => {
    if (!silent) loading.value = true
    try {
      const [taskResponse, settingResponse] = await Promise.all([
        api.get('/r2/tasks', { params: query }), api.get('/r2/settings'),
      ])
      tasks.value = taskResponse.data.data.items
      total.value = taskResponse.data.data.total
      Object.assign(counts, { queued: 0, running: 0, completed: 0, failed: 0 },
        taskResponse.data.data.counts || {})
      Object.assign(groupCounts.video, { queued: 0, running: 0, completed: 0, failed: 0 },
        taskResponse.data.data.groupCounts?.video || {})
      Object.assign(groupCounts.other, { queued: 0, running: 0, completed: 0, failed: 0 },
        taskResponse.data.data.groupCounts?.other || {})
      settings.value = settingResponse.data.data
    } catch (error: any) {
      if (!silent) ElMessage.error(error.message)
    } finally { if (!silent) loading.value = false }
  })()
  loadPromise = request
  try { await request }
  finally { if (loadPromise === request) loadPromise = undefined }
}
function search() { query.page = 1; table.value?.clearSelection(); selected.value = []; load() }
async function retry(ids: string[]) {
  if (!ids.length) return
  try {
    const result = (await api.post('/r2/tasks/retry', { ids })).data.data
    ElMessage.success(`已重新排队 ${result.retried.length} 个任务`)
    await load(true)
  } catch (error: any) { ElMessage.error(error.message) }
}
async function remove(ids: string[]) {
  if (!ids.length) return
  await ElMessageBox.confirm(`删除 ${ids.length} 条R2同步日志？R2对象和本地资源都会保留。`, '删除日志', { type: 'warning' })
  try {
    const result = (await api.post('/r2/tasks/delete', { ids })).data.data
    ElMessage.success(`已删除 ${result.deleted.length} 条日志`)
    table.value?.clearSelection(); selected.value = []
    await load(true)
  } catch (error: any) { ElMessage.error(error.message) }
}
function stopPolling() {
  pollingGeneration += 1
  if (timer) window.clearTimeout(timer)
  timer = undefined
}
async function poll(generation: number, silent = false) {
  if (!props.active || generation !== pollingGeneration) return
  await load(silent)
  if (props.active && generation === pollingGeneration) {
    timer = window.setTimeout(() => void poll(generation, true), 2500)
  }
}
function startPolling() {
  stopPolling()
  if (!props.active) return
  const generation = pollingGeneration
  void poll(generation)
}
watch(() => props.active, startPolling, { immediate: true })
onBeforeUnmount(stopPolling)
</script>

<template>
  <el-card shadow="never" class="panel">
    <template #header>
      <div class="panel-head">
        <div><h2>CF R2资源同步日志</h2><p>统一查看采集资源与上传资源的异步同步进度，不阻塞本地下载、上传和视频处理。</p></div>
        <div class="panel-actions">
          <el-tag v-if="!settings.enabled" type="info" size="large">未启用</el-tag>
          <template v-else>
            <el-tag :type="settings.videoConcurrency===0?'warning':'success'" size="large">视频{{settings.videoConcurrency===0?'已暂停':`并发 ${settings.videoConcurrency}`}}</el-tag>
            <el-tag :type="settings.otherConcurrency===0?'warning':'success'" size="large">其他{{settings.otherConcurrency===0?'已暂停':`并发 ${settings.otherConcurrency}`}}</el-tag>
          </template>
          <el-tag v-if="settings.autoUpload" type="success" effect="plain">上传自动同步</el-tag>
          <el-tag v-if="settings.autoCollection" type="success" effect="plain">采集自动同步</el-tag>
          <span class="secondary-cell">配置请在顶部“系统配置”中修改</span>
        </div>
      </div>
    </template>

    <div class="stat-grid ffmpeg-stats">
      <div><span>同步中</span><strong>{{counts.running}}</strong></div>
      <div><span>等待中</span><strong>{{counts.queued}}</strong></div>
      <div><span>已完成</span><strong>{{counts.completed}}</strong></div>
      <div><span>失败</span><strong>{{counts.failed}}</strong></div>
    </div>
    <div class="r2-group-counts secondary-cell">
      视频组：同步中 {{groupCounts.video.running}} · 等待 {{groupCounts.video.queued}} · 失败 {{groupCounts.video.failed}}
      <span>其他组：同步中 {{groupCounts.other.running}} · 等待 {{groupCounts.other.queued}} · 失败 {{groupCounts.other.failed}}</span>
    </div>

    <div class="filters ffmpeg-filters">
      <el-input v-model="query.keyword" clearable :prefix-icon="Search" placeholder="搜索文件名、资源ID或对象路径" @keyup.enter="search" @clear="search"/>
      <el-select v-model="query.status" clearable placeholder="全部状态" @change="search">
        <el-option label="等待同步" value="queued"/><el-option label="同步中" value="running"/>
        <el-option label="已完成" value="completed"/><el-option label="失败" value="failed"/>
      </el-select>
      <el-select v-model="query.resource_type" clearable placeholder="全部来源" @change="search">
        <el-option label="采集资源" value="collection"/><el-option label="上传资源" value="upload"/>
      </el-select>
      <el-select v-model="query.sync_group" clearable placeholder="全部同步组" @change="search">
        <el-option label="视频组" value="video"/><el-option label="其他组" value="other"/>
      </el-select>
      <el-button :icon="Refresh" @click="load()">刷新</el-button>
      <el-button :icon="RefreshLeft" :disabled="!retryable.length" @click="retry(retryable.map(item=>item.id))">批量重试（{{retryable.length}}）</el-button>
      <el-button type="danger" plain :icon="Delete" :disabled="!deletable.length" @click="remove(deletable.map(item=>item.id))">删除日志（{{deletable.length}}）</el-button>
      <el-button type="primary" :icon="Search" @click="search">搜索</el-button>
    </div>

    <el-table ref="table" v-loading="loading" :data="tasks" row-key="id"
              empty-text="暂无R2同步日志" @selection-change="selected=$event">
      <el-table-column type="selection" width="48" reserve-selection/>
      <el-table-column label="同步组" width="92"><template #default="{row}"><el-tag :type="row.syncGroup==='video'?'primary':'success'" effect="plain">{{row.syncGroup==='video'?'视频':'其他'}}</el-tag></template></el-table-column>
      <el-table-column label="资源" min-width="350"><template #default="{row}"><div class="primary-cell line-clamp">{{row.title || row.fileName}}</div><div class="secondary-cell"><el-tag size="small" effect="plain">{{row.resourceType==='collection'?'采集资源':'上传资源'}}</el-tag> {{row.source || row.fileType}} · {{row.resourceId}}</div><div class="secondary-cell line-clamp">{{row.objectKey || '—'}}</div></template></el-table-column>
      <el-table-column label="状态与进度" min-width="270"><template #default="{row}"><div class="task-status-line"><el-tag :type="statusType(row.status)">{{statusText(row.status,row.nextRetryAt)}}</el-tag><span>{{Math.round(row.progress)}}%</span></div><el-progress :percentage="Math.round(row.progress)" :status="row.status==='failed'?'exception':row.status==='completed'?'success':undefined"/><div v-if="row.nextRetryAt" class="secondary-cell">第 {{row.retryCount}} / {{row.maxAutoRetries}} 次自动重试 · {{formatTime(row.nextRetryAt)}}</div><div v-if="row.error" class="error-text">{{row.error}}</div><div v-else class="secondary-cell">{{row.message}}</div></template></el-table-column>
      <el-table-column label="文件" width="170"><template #default="{row}"><div>{{row.syncedFiles}} / {{row.totalFiles}} 个</div><div class="secondary-cell">{{formatBytes(row.syncedBytes)}} / {{formatBytes(row.totalBytes)}}</div></template></el-table-column>
      <el-table-column label="R2地址" min-width="260"><template #default="{row}"><a v-if="row.publicUrl" :href="row.publicUrl" target="_blank" rel="noopener" class="table-link">{{row.publicUrl}}</a><span v-else class="secondary-cell">未配置公开地址</span></template></el-table-column>
      <el-table-column label="更新" width="175"><template #default="{row}">{{formatTime(row.updatedAt)}}</template></el-table-column>
      <el-table-column label="操作" width="130" fixed="right"><template #default="{row}"><el-button v-if="row.status==='failed'" type="primary" link @click="retry([row.id])">重试</el-button><el-button v-if="row.status!=='running'" type="danger" link @click="remove([row.id])">删除</el-button></template></el-table-column>
    </el-table>
    <div class="pagination"><el-pagination v-model:current-page="query.page" v-model:page-size="query.page_size" :page-sizes="[20,50,100]" layout="total, sizes, prev, pager, next, jumper" :total="total" @current-change="load(true)" @size-change="query.page=1;load(true)"/></div>
  </el-card>

</template>
