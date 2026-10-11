<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { api, type FfmpegProcess, type SystemMetricPoint, type SystemMetrics } from '../api'
import TrendChart from './TrendChart.vue'

const props = withDefaults(defineProps<{ active: boolean, showOverview?: boolean }>(), { showOverview: true })
const loading = ref(false), metrics = ref<SystemMetrics>(), points = ref<SystemMetricPoint[]>([])
const processes = ref<FfmpegProcess[]>([]), range = ref(60)
const ranges = [{ label: '5分钟', value: 5 }, { label: '15分钟', value: 15 }, { label: '1小时', value: 60 }, { label: '6小时', value: 360 }, { label: '24小时', value: 1440 }, { label: '3天', value: 4320 }]
let timer: number | undefined, historyTick = 0
let currentPromise: Promise<void> | undefined
let historyPromise: Promise<void> | undefined
let historyLoadedRange = 0
let pollingGeneration = 0

const formatBytes = (bytes?: number) => {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']; let value = bytes, index = 0
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1 }
  return `${value >= 100 ? value.toFixed(0) : value >= 10 ? value.toFixed(1) : value.toFixed(2)} ${units[index]}`
}
const formatUptime = (seconds?: number) => {
  if (!seconds) return '0秒'
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600), minutes = Math.floor(seconds % 3600 / 60)
  return days ? `${days}天 ${hours}小时` : hours ? `${hours}小时 ${minutes}分钟` : `${minutes}分钟`
}
const metricLevel = (value?: number) => (value || 0) >= 90 ? 'danger' : (value || 0) >= 70 ? 'warning' : 'normal'
const stageText = (value?: string) => ({ probing: '读取信息', compressing: '压缩与切片', segmenting: '视频切片', generating_cover: '生成封面', generating_preview: '生成预览', generating_vtt: '生成进度预览' }[value || ''] || value || '运行中')

async function loadCurrent(silent = false) {
  if (currentPromise) return currentPromise
  const request = (async () => {
    if (!silent) loading.value = true
    try {
      const [metricResponse, processResponse] = await Promise.all([
        api.get('/system/metrics'), api.get('/system/ffmpeg-processes'),
      ])
      metrics.value = metricResponse.data.data
      processes.value = processResponse.data.data.items
    } finally { if (!silent) loading.value = false }
  })()
  currentPromise = request
  try { await request }
  finally { if (currentPromise === request) currentPromise = undefined }
}
async function loadHistory() {
  if (historyPromise) return historyPromise
  const requestedRange = range.value
  const request = (async () => {
    const response = await api.get('/system/metrics/history', { params: { minutes: requestedRange } })
    if (range.value === requestedRange) {
      points.value = response.data.data.items
      historyLoadedRange = requestedRange
    }
  })()
  historyPromise = request
  try { await request }
  finally { if (historyPromise === request) historyPromise = undefined }
}
async function refreshAll() { await Promise.all([loadCurrent(), loadHistory()]) }
async function changeRange(value: number) {
  range.value = value
  await loadHistory()
  if (range.value === value && historyLoadedRange !== value) await loadHistory()
}
function stopPolling() {
  pollingGeneration += 1
  if (timer) window.clearTimeout(timer)
  timer = undefined
}
async function poll(generation: number, initial = false) {
  if (!props.active || generation !== pollingGeneration) return
  try {
    if (initial) {
      await refreshAll()
    } else {
      await loadCurrent(true)
      historyTick += 1
      if (historyTick >= 12) { historyTick = 0; await loadHistory() }
    }
  } catch {
    // 下一轮继续刷新，避免一次瞬时网络错误永久停止监控。
  }
  if (props.active && generation === pollingGeneration) {
    timer = window.setTimeout(() => void poll(generation), 2500)
  }
}
function startPolling() {
  stopPolling()
  if (!props.active) return
  historyTick = 0
  const generation = pollingGeneration
  void poll(generation, true)
}
watch(() => props.active, startPolling, { immediate: true })
onBeforeUnmount(stopPolling)
</script>

<template>
  <div v-loading="loading">
    <el-card v-if="props.showOverview" shadow="never" class="panel monitor-overview">
      <template #header><div class="panel-head"><div><h2>运行监控</h2><p>容器资源、磁盘容量和FFmpeg进程实时状态；趋势数据保留最近3天。</p></div><el-button :icon="Refresh" @click="refreshAll">刷新</el-button></div></template>
      <div class="monitor-cards">
        <div class="monitor-card" :class="metricLevel(metrics?.cpuPercent)"><span>CPU</span><strong>{{(metrics?.cpuPercent || 0).toFixed(1)}}%</strong><small>{{metrics?.cpuCores || 0}} 核<span v-if="metrics?.cpuLimited"> · 容器限制</span></small></div>
        <div class="monitor-card" :class="metricLevel(metrics?.memoryPercent)"><span>内存</span><strong>{{(metrics?.memoryPercent || 0).toFixed(1)}}%</strong><small>{{formatBytes(metrics?.memoryUsed)}} / {{formatBytes(metrics?.memoryLimit)}}<span v-if="metrics && !metrics.memoryLimited"> · 未限制</span></small></div>
        <div class="monitor-card" :class="metricLevel(metrics?.diskPercent)"><span>下载磁盘</span><strong>{{(metrics?.diskPercent || 0).toFixed(1)}}%</strong><small>剩余 {{formatBytes(metrics?.diskFree)}}</small></div>
        <div class="monitor-card" :class="metricLevel(metrics?.ffmpegCpuPercent)"><span>FFmpeg</span><strong>{{metrics?.ffmpegCount || 0}} 个</strong><small>CPU {{(metrics?.ffmpegCpuPercent || 0).toFixed(1)}}% · {{formatBytes(metrics?.ffmpegMemory)}}</small></div>
        <div class="monitor-card normal"><span>API服务</span><strong>{{formatUptime(metrics?.serviceUptime)}}</strong><small>CPU {{(metrics?.serviceCpuPercent || 0).toFixed(1)}}% · {{formatBytes(metrics?.serviceMemory)}}</small></div>
      </div>
    </el-card>

    <el-card shadow="never" class="panel">
      <template #header><div class="panel-head"><div><h2>资源趋势</h2><p>采样间隔 {{metrics?.sampleInterval || 5}} 秒，超过1小时自动聚合，数据库仅保留最近3天。</p></div><el-radio-group :model-value="range" size="small" @change="changeRange(Number($event))"><el-radio-button v-for="item in ranges" :key="item.value" :label="item.value" :value="item.value">{{item.label}}</el-radio-button></el-radio-group></div></template>
      <div class="monitor-charts"><TrendChart title="CPU使用率" color="#5965e8" value-key="cpuPercent" :points="points"/><TrendChart title="内存使用率" color="#20a174" value-key="memoryPercent" :points="points"/><TrendChart title="磁盘使用率" color="#d99316" value-key="diskPercent" :points="points"/></div>
    </el-card>

    <el-card shadow="never" class="panel">
      <template #header><div class="panel-head"><div><h2>FFmpeg进程</h2><p>显示当前进程的CPU、内存、处理速度和对应上传任务。</p></div><el-tag type="primary">{{processes.length}} 个进程</el-tag></div></template>
      <el-table :data="processes" empty-text="当前没有运行中的FFmpeg进程">
        <el-table-column label="任务" min-width="320"><template #default="{row}"><div class="primary-cell">{{row.fileName || row.taskId || row.command}}</div><div class="secondary-cell">PID {{row.pid}}<template v-if="row.taskId"> · {{row.taskId}}</template></div></template></el-table-column>
        <el-table-column label="阶段" width="150"><template #default="{row}"><el-tag>{{stageText(row.stage)}}</el-tag></template></el-table-column>
        <el-table-column label="任务进度" width="180"><template #default="{row}"><el-progress :percentage="Math.round(row.progress || 0)"/><div v-if="row.outTime" class="secondary-cell">已处理 {{row.outTime}}</div></template></el-table-column>
        <el-table-column label="CPU" width="110"><template #default="{row}">{{row.cpuPercent.toFixed(1)}}%</template></el-table-column>
        <el-table-column label="内存" width="130"><template #default="{row}">{{formatBytes(row.memoryUsed)}}</template></el-table-column>
        <el-table-column label="速度 / 帧" width="145"><template #default="{row}"><div>{{row.speed || '—'}}</div><div class="secondary-cell">{{row.frame != null ? `${row.frame} 帧` : '—'}}</div></template></el-table-column>
      </el-table>
    </el-card>
  </div>
</template>
