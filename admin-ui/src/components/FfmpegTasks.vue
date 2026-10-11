<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Refresh, Search, VideoPause, VideoPlay } from '@element-plus/icons-vue'
import { api, type FfmpegSettings, type FfmpegTask } from '../api'
import TaskThumbnail from './TaskThumbnail.vue'

const props = defineProps<{ active: boolean }>()

const loading = ref(false), total = ref(0)
const tasks = ref<FfmpegTask[]>([]), selected = ref<FfmpegTask[]>([])
const table = ref()
const settings = ref<FfmpegSettings>({ concurrency: 1, maxConcurrency: 16, paused: false, running: 0, queued: 0, completed: 0, failed: 0, cancelled: 0 })
const query = reactive({ page: 1, page_size: 20, keyword: '', status: '' })
let timer: number | undefined
let loadPromise: Promise<void> | undefined
let pollingGeneration = 0

const retryable = computed(() => selected.value.filter(item => ['failed', 'cancelled'].includes(item.status)))
const cancellable = computed(() => selected.value.filter(item => item.status === 'queued'))
const formatBytes = (value?: number) => {
  if (!value) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']; let size = value, index = 0
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1 }
  return `${size >= 100 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`
}
const formatBitRate = (value?: number) => value ? `${(value / 1_000_000).toFixed(2)} Mbps` : '—'
const formatTime = (value?: number) => value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'
const elapsed = (row: FfmpegTask) => {
  if (!row.startedAt) return '—'
  const seconds = Math.max(0, Math.round((row.completedAt || Date.now() / 1000) - row.startedAt))
  const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60), rest = seconds % 60
  return hours ? `${hours}时${minutes}分${rest}秒` : minutes ? `${minutes}分${rest}秒` : `${rest}秒`
}
const statusText = (value: string) => ({
  queued: '等待调度', probing: '读取信息', compressing: '压缩与切片', segmenting: '视频切片',
  generating_cover: '生成封面', generating_preview: '生成预览', generating_vtt: '生成进度预览',
  completed: '已完成', failed: '失败', cancelled: '已取消',
}[value] || value)
const statusType = (value: string) => ({ completed: 'success', failed: 'danger', queued: 'warning', cancelled: 'info' }[value] || 'primary') as any
async function load(silent = false) {
  if (loadPromise) return loadPromise
  const request = (async () => {
    if (!silent) loading.value = true
    try {
      const [taskResponse, settingResponse] = await Promise.all([
        api.get('/ffmpeg/tasks', { params: query }), api.get('/ffmpeg/settings'),
      ])
      tasks.value = taskResponse.data.data.items
      total.value = taskResponse.data.data.total
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
    const result = (await api.post('/ffmpeg/tasks/retry', { identifiers: ids })).data.data
    ElMessage.success(`已重新排队 ${result.retried.length} 个任务`)
    await load(true)
  } catch (error: any) { ElMessage.error(error.message) }
}
async function cancel(ids: string[]) {
  if (!ids.length) return
  try {
    const result = (await api.post('/ffmpeg/tasks/cancel', { identifiers: ids })).data.data
    ElMessage.success(`已取消 ${result.cancelled.length} 个排队任务`)
    await load(true)
  } catch (error: any) { ElMessage.error(error.message) }
}
async function remove(row: FfmpegTask) {
  await ElMessageBox.confirm(`删除“${row.fileName}”的 FFmpeg 任务记录？上传资源不会删除。`, '删除记录', { type: 'warning' })
  try { await api.delete(`/ffmpeg/tasks/${row.id}`); ElMessage.success('任务记录已删除'); await load(true) }
  catch (error: any) { ElMessage.error(error.message) }
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
        <div><h2>FFmpeg 任务</h2><p>统一调度上传视频的探测、压缩、HLS 切片、封面和预览生成。</p></div>
        <div class="panel-actions ffmpeg-setting">
          <el-tag v-if="settings.paused" type="warning" size="large">调度已暂停</el-tag>
          <el-tag v-else type="success" size="large">调度中 · 并发 {{settings.concurrency}}</el-tag>
          <span class="secondary-cell">配置请在顶部“系统配置”中修改</span>
        </div>
      </div>
    </template>

    <div class="stat-grid ffmpeg-stats">
      <div><span>运行中</span><strong>{{settings.running}}</strong></div>
      <div><span>等待中</span><strong>{{settings.queued}}</strong></div>
      <div><span>已完成</span><strong>{{settings.completed}}</strong></div>
      <div><span>失败</span><strong>{{settings.failed}}</strong></div>
      <div><span>已取消</span><strong>{{settings.cancelled}}</strong></div>
    </div>

    <div class="filters ffmpeg-filters">
      <el-input v-model="query.keyword" clearable :prefix-icon="Search" placeholder="搜索文件名或资源 ID" @keyup.enter="search" @clear="search"/>
      <el-select v-model="query.status" clearable placeholder="全部状态" @change="search">
        <el-option label="等待调度" value="queued"/><el-option label="运行中" value="running"/>
        <el-option label="压缩视频" value="compressing"/><el-option label="视频切片" value="segmenting"/>
        <el-option label="已完成" value="completed"/><el-option label="失败" value="failed"/>
        <el-option label="已取消" value="cancelled"/>
      </el-select>
      <el-button :icon="Refresh" @click="load()">刷新</el-button>
      <el-button :icon="VideoPlay" :disabled="!retryable.length" @click="retry(retryable.map(item => item.id))">批量重试（{{retryable.length}}）</el-button>
      <el-button :icon="VideoPause" :disabled="!cancellable.length" @click="cancel(cancellable.map(item => item.id))">批量取消（{{cancellable.length}}）</el-button>
      <el-button type="primary" :icon="Search" @click="search">搜索</el-button>
    </div>

    <el-table ref="table" v-loading="loading" :data="tasks" row-key="id" @selection-change="selected=$event" empty-text="暂无 FFmpeg 任务">
      <el-table-column type="selection" width="48" reserve-selection/>
      <el-table-column label="视频" min-width="390">
        <template #default="{row}"><div class="resource-cell"><TaskThumbnail :cover="row.coverPath"/><div class="resource-copy"><div class="primary-cell line-clamp">{{row.fileName}}</div><div class="secondary-cell">{{row.uploadIdentifier}}</div><div class="secondary-cell"><template v-if="row.durationText">{{row.durationText}} · </template><template v-if="row.width && row.height">{{row.width}}×{{row.height}} · </template>{{row.videoCodec || '—'}} / {{row.audioCodec || '—'}}</div></div></div></template>
      </el-table-column>
      <el-table-column label="原视频" width="160"><template #default="{row}"><div>{{formatBytes(row.originalSize)}}</div><div class="secondary-cell">{{formatBitRate(row.bitRate)}}</div></template></el-table-column>
      <el-table-column label="压缩判断" min-width="210"><template #default="{row}"><el-tag v-if="row.compressionRequired===true" type="warning">需要压缩</el-tag><el-tag v-else-if="row.compressionRequired===false" type="success">无需压缩</el-tag><span v-else>等待探测</span><div v-if="row.compressionReason" class="secondary-cell">{{row.compressionReason}}</div><div v-if="row.status==='completed' && row.processedSize" class="secondary-cell">处理后 {{formatBytes(row.processedSize)}}<template v-if="row.compressed"> · 减少 {{row.compressionRatio || 0}}%</template></div></template></el-table-column>
      <el-table-column label="状态与进度" min-width="230"><template #default="{row}"><div class="task-status-line"><el-tag :type="statusType(row.status)">{{statusText(row.status)}}</el-tag><span>{{Math.round(row.progress)}}%</span></div><el-progress :percentage="Math.round(row.progress)" :status="row.status==='failed'?'exception':row.status==='completed'?'success':undefined"/><div v-if="row.error" class="error-text">{{row.error}}</div><div v-else class="secondary-cell">{{row.message}}</div></template></el-table-column>
      <el-table-column label="时间" width="185"><template #default="{row}"><div>{{formatTime(row.updatedAt)}}</div><div class="secondary-cell">耗时 {{elapsed(row)}}</div></template></el-table-column>
      <el-table-column label="操作" width="150" fixed="right"><template #default="{row}"><el-button v-if="['failed','cancelled'].includes(row.status)" type="primary" link @click="retry([row.id])">重试</el-button><el-button v-if="row.status==='queued'" type="warning" link @click="cancel([row.id])">取消</el-button><el-button v-if="['completed','failed','cancelled'].includes(row.status)" type="danger" link :icon="Delete" @click="remove(row)">删除</el-button></template></el-table-column>
    </el-table>
    <div class="pagination"><el-pagination v-model:current-page="query.page" v-model:page-size="query.page_size" :page-sizes="[20,50,100]" layout="total, sizes, prev, pager, next, jumper" :total="total" @current-change="load(true)" @size-change="query.page=1;load(true)"/></div>
  </el-card>

</template>
