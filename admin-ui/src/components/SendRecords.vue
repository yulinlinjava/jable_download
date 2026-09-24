<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import { api } from '../api'

interface SendRecord {
  id: string
  run_id: string
  task_id: string
  source: string
  video_id: string
  title: string
  status: 'queued' | 'resolving' | 'sending' | 'succeeded' | 'failed' | 'resolved'
  remote_id?: string
  message?: string
  site_id: string
  site_name: string
  scope: 'all' | 'selected'
  created: number
}

interface DownloadNode {
  id: string
  name: string
  resolve_media: boolean
}

const loading = ref(false)
const operating = ref(false)
const records = ref<SendRecord[]>([])
const selected = ref<SendRecord[]>([])
const total = ref(0)
const table = ref()
const nodes = ref<DownloadNode[]>([])
let refreshTimer: number | undefined
const query = reactive({ page: 1, page_size: 20, keyword: '', status: '', site_id: '' })
const selectedFailed = computed(() => selected.value.filter(item => item.status === 'failed'))
const formatTime = (value?: number) => value
  ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'
const statusText = (value: string) => ({ queued: '等待发送', resolving: '获取资源', sending: '发送中', succeeded: '成功', failed: '失败', resolved: '重试成功' }[value] || value)
const statusType = (value: string) => value === 'failed' ? 'danger' : ['queued', 'resolving', 'sending'].includes(value) ? 'warning' : 'success'

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const params: Record<string, unknown> = {
      page: query.page, page_size: query.page_size,
    }
    if (query.keyword.trim()) params.keyword = query.keyword.trim()
    if (query.status) params.status = query.status
    if (query.site_id) params.site_id = query.site_id
    const response = await api.get('/resource-sync/records', { params })
    records.value = response.data.data.items
    total.value = response.data.data.total
    if (!silent) {
      selected.value = []
      table.value?.clearSelection()
    }
  } catch (error: any) {
    if (!silent) ElMessage.error(error.message)
  } finally {
    loading.value = false
  }
}

async function loadNodes() {
  try {
    const response = await api.get('/resource-sync/sites')
    nodes.value = response.data.data.filter((item: DownloadNode) => item.resolve_media)
  } catch (error: any) {
    ElMessage.error(error.message)
  }
}

function search() {
  query.page = 1
  load()
}

function changePage(page: number) {
  query.page = page
  load()
}

function changeSize(size: number) {
  query.page_size = size
  query.page = 1
  load()
}

function retryMessage(result: any) {
  const base = `已重新加入等待发送 ${result.queued || 0} 条，跳过 ${result.skipped || 0} 条`
  return result.errors?.length ? `${base}；${result.errors[0]}` : base
}

async function retryRecords(ids: string[], all = false) {
  if (!all && !ids.length) return ElMessage.warning('请选择失败记录')
  try {
    const text = all
      ? '将重试所有失败发送记录，同一节点的同一资源只发送一次，是否继续？'
      : `重试选中的 ${ids.length} 条失败记录？`
    await ElMessageBox.confirm(text, all ? '全部重试' : '批量重试', { type: 'warning' })
  } catch (error) {
    return
  }
  operating.value = true
  try {
    const response = await api.post('/resource-sync/records/retry', { ids: all ? [] : ids, all }, { timeout: 0 })
    const result = response.data.data
    if (!result.retried) ElMessage.info('当前没有需要重试的失败记录')
    else if (result.failed) ElMessage.warning(retryMessage(result))
    else ElMessage.success(retryMessage(result))
    await load(true)
  } catch (error: any) {
    ElMessage.error(error.message)
  } finally {
    operating.value = false
  }
}

async function deleteRecords(ids: string[]) {
  if (!ids.length) return ElMessage.warning('请选择要删除的发送记录')
  try {
    await ElMessageBox.confirm(`删除选中的 ${ids.length} 条发送记录？不会删除本地或节点资源。`, '删除发送记录', { type: 'warning' })
  } catch (error) {
    return
  }
  operating.value = true
  try {
    const result = (await api.post('/resource-sync/records/delete', { ids })).data.data
    const message = `已删除 ${result.deleted} 条，执行中跳过 ${result.active || 0} 条`
    result.active ? ElMessage.warning(message) : ElMessage.success(message)
    if (records.value.length === result.deleted && query.page > 1) query.page -= 1
    await load(true)
  } catch (error: any) {
    ElMessage.error(error.message)
  } finally {
    operating.value = false
  }
}

const reload = () => load(true)
onMounted(() => {
  load()
  loadNodes()
  window.addEventListener('resource-sync-records-changed', reload)
  refreshTimer = window.setInterval(() => load(true), 3000)
})
onBeforeUnmount(() => {
  window.removeEventListener('resource-sync-records-changed', reload)
  if (refreshTimer) window.clearInterval(refreshTimer)
})
</script>

<template>
  <el-card shadow="never" class="records-panel">
    <template #header>
      <div class="records-head">
        <div><h2>发送记录</h2><p>按资源查看调度至下载节点的结果，失败记录可以重试。</p></div>
        <div class="head-actions">
          <el-button type="danger" plain :disabled="!selected.length || operating" @click="deleteRecords(selected.map(item => item.id))">批量删除（{{selected.length}}）</el-button>
          <el-button type="warning" plain :disabled="!selectedFailed.length || operating" @click="retryRecords(selectedFailed.map(item => item.id))">多选重试（{{selectedFailed.length}}）</el-button>
          <el-button type="primary" :loading="operating" @click="retryRecords([], true)">全部重试</el-button>
        </div>
      </div>
    </template>

    <div class="filters">
      <el-input v-model="query.keyword" clearable :prefix-icon="Search" placeholder="搜索标题、番号、节点或来源" @keyup.enter="search" @clear="search"/>
      <el-select v-model="query.site_id" clearable filterable placeholder="全部下载节点" @change="search">
        <el-option v-for="node in nodes" :key="node.id" :label="node.name" :value="node.id"/>
      </el-select>
      <el-select v-model="query.status" clearable placeholder="全部状态" @change="search"><el-option label="等待发送" value="queued"/><el-option label="获取资源" value="resolving"/><el-option label="发送中" value="sending"/><el-option label="成功" value="succeeded"/><el-option label="失败" value="failed"/><el-option label="重试成功" value="resolved"/></el-select>
      <el-button type="primary" :icon="Search" @click="search">搜索</el-button>
      <el-button :icon="Refresh" @click="load()">刷新</el-button>
    </div>

    <el-table ref="table" v-loading="loading" :data="records" row-key="id" empty-text="暂无发送记录" @selection-change="selected=$event">
      <el-table-column type="selection" width="48" reserve-selection/>
      <el-table-column prop="title" label="标题" min-width="260" show-overflow-tooltip><template #default="{row}"><div class="record-title">{{row.title || row.video_id}}</div></template></el-table-column>
      <el-table-column label="来源 / 番号" min-width="170"><template #default="{row}"><div>{{row.source}}</div><div class="secondary">{{row.video_id}}</div></template></el-table-column>
      <el-table-column prop="site_name" label="下载节点" min-width="140" show-overflow-tooltip/>
      <el-table-column label="范围" width="80"><template #default="{row}">{{row.scope==='all'?'全部':'已选'}}</template></el-table-column>
      <el-table-column label="状态" width="90"><template #default="{row}"><el-tag :type="statusType(row.status)">{{statusText(row.status)}}</el-tag></template></el-table-column>
      <el-table-column prop="message" label="节点返回消息" min-width="210" show-overflow-tooltip><template #default="{row}">{{row.message || statusText(row.status)}}</template></el-table-column>
      <el-table-column label="发送时间" width="175"><template #default="{row}">{{formatTime(row.created)}}</template></el-table-column>
      <el-table-column label="操作" width="125" fixed="right"><template #default="{row}"><el-button v-if="row.status==='failed'" link type="primary" :disabled="operating" @click="retryRecords([row.id])">重试</el-button><el-button link type="danger" :disabled="operating" @click="deleteRecords([row.id])">删除</el-button></template></el-table-column>
    </el-table>

    <div class="pagination"><el-pagination v-model:current-page="query.page" v-model:page-size="query.page_size" :page-sizes="[20,50,100]" layout="total, sizes, prev, pager, next, jumper" :total="total" @current-change="changePage" @size-change="changeSize"/></div>
  </el-card>
</template>

<style scoped>
.records-panel { border: 0; border-radius: 16px; }
.records-head { display: flex; align-items: center; justify-content: space-between; gap: 18px; }
.records-head h2 { margin: 0 0 5px; font-size: 18px; }
.records-head p { margin: 0; color: #64748b; }
.head-actions, .filters { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.filters { margin-bottom: 16px; }
.filters .el-input { width: 310px; }
.filters .el-select { width: 190px; }
.record-title { font-weight: 600; color: #172033; }
.secondary { margin-top: 3px; color: #64748b; font-size: 12px; }
.pagination { display: flex; justify-content: flex-end; margin-top: 18px; }
@media (max-width: 900px) { .records-head { align-items: flex-start; flex-direction: column; } }
</style>
