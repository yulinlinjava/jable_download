<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CirclePlus, Collection, Download, Film, Key, Odometer, Refresh, Search, Setting, VideoPlay } from '@element-plus/icons-vue'
import { api, type CollectionItem, type CollectionTask, type DownloadTask } from '../api'
import { useTaskStore } from '../stores/tasks'
import TaskThumbnail from '../components/TaskThumbnail.vue'
import VideoPlayer from '../components/VideoPlayer.vue'

const store = useTaskStore()
const activeTab = ref('collection')
const formVisible = ref(false), keyVisible = ref(false), playerVisible = ref(false)
const playing = ref<DownloadTask>(), editing = ref<string>()
const selectedCollections = ref<CollectionTask[]>([]), selectedResources = ref<CollectionItem[]>([])
const keyValue = ref(localStorage.getItem('jable-api-key') || '')
const form = reactive({ name: '', url: '', batch_size: 1, scheduled_enabled: false })
let timer: number | undefined
const runningCollections = computed(() => store.collections.filter(x => ['queued', 'running'].includes(x.status)).length)
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
const statusText = (value: string) => ({ pending: '待下载', completed: '已完成', failed: '失败', downloading: '下载中', queued: '排队中', idle: '空闲', running: '采集中', deleting: '删除中' }[value] || value)
const selectableResource = (row: CollectionItem) => row.kind === 'collection' && ['pending', 'failed'].includes(row.status)

async function refresh(silent = false) { try { await store.refresh(silent) } catch (e: any) { if (!silent) ElMessage.error(e.message) } }
function openCreate() { editing.value = undefined; Object.assign(form, { name: '', url: '', batch_size: 1, scheduled_enabled: false }); formVisible.value = true }
function openEdit(row: CollectionTask) { editing.value = row.id; Object.assign(form, { name: row.name, url: row.url, batch_size: row.batch_size, scheduled_enabled: row.scheduled_enabled }); formVisible.value = true }
async function saveCollection() {
  if (!form.url.trim()) return ElMessage.warning('请输入列表链接')
  try {
    editing.value ? await api.put(`/collections/${editing.value}`, form) : await api.post('/collections', form)
    formVisible.value = false; ElMessage.success(editing.value ? '任务已更新' : '任务已创建'); await refresh()
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
    play(task)
  } catch (e: any) { ElMessage.error(e.message) }
}
async function scheduleResource(row: CollectionItem) {
  try {
    if (row.kind === 'download') await api.post('/download', { url: row.url })
    else await api.post(`/collection-items/${row.id}/download`)
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
  try { await api.delete(`/download/${row.download_task_id}`); ElMessage.success('本地资源已删除'); await refresh() }
  catch (e: any) { ElMessage.error(e.message) }
}
function searchResources() { store.resourceQuery.page = 1; refresh() }
function changeResourcePage(page: number) { store.resourceQuery.page = page; refresh(true) }
function changeResourceSize(size: number) { store.resourceQuery.page_size = size; store.resourceQuery.page = 1; refresh(true) }
async function saveScheduler() {
  try {
    const response = await api.put('/settings/scheduler', { enabled: store.settings.enabled, concurrency: store.settings.concurrency })
    store.settings = response.data.data; ElMessage.success('调度设置已保存')
  } catch (e: any) { ElMessage.error(e.message); await refresh(true) }
}
function saveKey() { const key = keyValue.value.trim(); key ? localStorage.setItem('jable-api-key', key) : localStorage.removeItem('jable-api-key'); keyVisible.value = false; refresh() }

onMounted(() => { refresh(); timer = window.setInterval(() => refresh(true), 2500) })
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div><div class="eyebrow">JABLE TV DOWNLOAD</div><h1>资源采集控制台</h1><p>管理列表采集、下载队列与本地视频。</p></div>
      <div class="header-actions"><el-button :icon="Key" circle @click="keyVisible=true"/><el-button :icon="Refresh" circle @click="refresh()"/></div>
    </header>
    <section class="stats">
      <div class="stat"><span class="stat-icon indigo"><Collection/></span><div><strong>{{ store.resourceTotal }}</strong><span>资源总数</span></div></div>
      <div class="stat"><span class="stat-icon amber"><Download/></span><div><strong>{{ store.activeDownloads }}</strong><span>等待 / 下载中</span></div></div>
      <div class="stat"><span class="stat-icon green"><Film/></span><div><strong>{{ store.completedDownloads }}</strong><span>已完成资源</span></div></div>
      <div class="stat"><span class="stat-icon blue"><Refresh/></span><div><strong>{{ runningCollections }}</strong><span>正在采集</span></div></div>
      <div class="stat"><span class="stat-icon violet"><Odometer/></span><div><strong>{{ formatSpeed(store.settings.download_speed) }}</strong><span>实时下载网速</span></div></div>
    </section>

    <el-tabs v-model="activeTab" class="main-tabs">
      <el-tab-pane label="列表采集" name="collection">
        <el-card shadow="never" class="panel">
          <template #header><div class="panel-head"><div><h2>列表采集</h2><p>执行后保存当前列表页全部视频信息，不会等待资源下载。</p></div><div class="panel-actions"><el-button :disabled="!selectedCollections.length" @click="bulkExecuteCollections">批量执行（{{selectedCollections.length}}）</el-button><el-button type="primary" :icon="CirclePlus" @click="openCreate">新建采集任务</el-button></div></div></template>
          <el-table :data="store.collections" v-loading="store.loading" empty-text="还没有采集任务" @selection-change="selectedCollections=$event">
            <el-table-column type="selection" width="48"/>
            <el-table-column label="任务" min-width="220"><template #default="{row}"><div class="primary-cell">{{row.name}}</div><div class="secondary-cell ellipsis">{{row.url}}</div></template></el-table-column>
            <el-table-column label="定时采集" width="105"><template #default="{row}"><el-tag :type="row.scheduled_enabled?'success':'info'" effect="plain">{{row.scheduled_enabled?'每天 18:00':'已关闭'}}</el-tag></template></el-table-column>
            <el-table-column label="状态" width="105"><template #default="{row}"><el-tag :type="statusType(row.status)" effect="light">{{statusText(row.status)}}</el-tag></template></el-table-column>
            <el-table-column label="上次结果" min-width="170"><template #default="{row}"><span v-if="row.last_run_at">共 {{row.last_total}}，新增 {{row.last_created}}，更新 {{row.last_skipped}}</span><span v-else>尚未执行</span><div v-if="row.last_error" class="error-text">{{row.last_error}}</div></template></el-table-column>
            <el-table-column label="上次执行" width="175"><template #default="{row}">{{formatTime(row.last_run_at)}}</template></el-table-column>
            <el-table-column label="操作" width="205" fixed="right"><template #default="{row}"><el-button type="primary" link :disabled="row.status!=='idle'" @click="execute(row)">立即执行</el-button><el-button link :disabled="row.status!=='idle'" @click="openEdit(row)">编辑</el-button><el-button type="danger" link :disabled="row.status!=='idle'" @click="removeCollection(row)">删除</el-button></template></el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="资源管理" name="resources">
        <el-card shadow="never" class="panel scheduler-panel">
          <div class="scheduler-content">
            <div class="scheduler-title"><span class="stat-icon indigo"><Setting/></span><div><h2>全局下载调度</h2><p>关闭后队列保留，正在下载的任务继续完成；开启后按并发数继续处理。</p></div></div>
            <div class="scheduler-controls"><div class="scheduler-state"><span>自动调度</span><el-switch v-model="store.settings.enabled" @change="saveScheduler"/></div><div class="scheduler-state"><span>下载并发</span><el-input-number v-model="store.settings.concurrency" :min="1" :max="10" @change="saveScheduler"/></div><el-tag type="primary" effect="light">运行 {{store.settings.running}} / {{store.settings.concurrency}}</el-tag></div>
          </div>
        </el-card>

        <el-card shadow="never" class="panel resources-panel">
          <template #header><div class="panel-head"><div><h2>资源管理</h2><p>采集信息、下载状态和本地资源合并展示，每个视频只显示一条。</p></div><div class="panel-actions"><el-button @click="deduplicateDownloads">按番号去重</el-button><el-button type="primary" plain :disabled="!selectedResources.length" @click="bulkScheduleResources">批量下载（{{selectedResources.length}}）</el-button><el-button type="primary" @click="downloadAllPending">全部下载</el-button></div></div></template>
          <div class="filters">
            <el-input v-model="store.resourceQuery.title" clearable :prefix-icon="Search" placeholder="标题或番号模糊搜索" @keyup.enter="searchResources" @clear="searchResources"/>
            <el-select v-model="store.resourceQuery.source" clearable placeholder="全部来源" @change="searchResources"><el-option label="Jable" value="jable"/><el-option label="123AV" value="123av"/></el-select>
            <el-select v-model="store.resourceQuery.status" clearable placeholder="全部状态" @change="searchResources"><el-option label="待下载" value="pending"/><el-option label="排队中" value="queued"/><el-option label="下载中" value="downloading"/><el-option label="已完成" value="completed"/><el-option label="失败" value="failed"/></el-select>
            <el-button type="primary" :icon="Search" @click="searchResources">搜索</el-button>
          </div>
          <el-table :data="store.resources" empty-text="暂无资源" @selection-change="selectedResources=$event" @row-click="playResource" :row-class-name="({row}) => row.status==='completed' ? 'playable-row' : ''">
            <el-table-column type="selection" width="48" :selectable="selectableResource"/>
            <el-table-column label="资源" min-width="500"><template #default="{row}"><div class="resource-cell"><TaskThumbnail v-if="row.download" :key="row.download.task_id" :task="row.download" @click.stop="playResource(row)"/><el-image v-else class="resource-cover" :src="row.cover" fit="cover"><template #error><div class="image-fallback"><Film/></div></template></el-image><div class="resource-copy"><div class="primary-cell line-clamp">{{row.title || row.video_id}}</div><div class="secondary-cell">{{row.source}} · {{row.video_id}}<span v-if="row.duration"> · {{row.duration}}</span></div><div v-if="row.download?.error" class="error-text">{{row.download.error}}</div><div v-else-if="row.download?.warnings?.length" class="warning-text">{{row.download.warnings.join('；')}}</div><div v-else-if="row.download?.message" class="secondary-cell">{{row.download.message}}</div></div></div></template></el-table-column>
            <el-table-column label="状态" width="105"><template #default="{row}"><el-tag :type="statusType(row.status)">{{statusText(row.status)}}</el-tag></template></el-table-column>
            <el-table-column label="下载进度" min-width="190"><template #default="{row}"><template v-if="row.download"><el-progress :percentage="Math.round(row.download.progress)" :status="row.status==='failed'?'exception':row.status==='completed'?'success':undefined"/><div class="secondary-cell">{{row.download.downloaded_segments}} / {{row.download.total_segments}} 分片 · {{formatBytes(row.download.downloaded_bytes)}}</div></template><span v-else class="secondary-cell">尚未加入下载队列</span></template></el-table-column>
            <el-table-column label="采集 / 更新" width="175"><template #default="{row}">{{formatTime(row.download?.updated_at || row.first_seen)}}</template></el-table-column>
            <el-table-column label="操作" width="165" fixed="right"><template #default="{row}"><el-button v-if="row.status==='completed'" type="primary" link :icon="VideoPlay" @click.stop="playResource(row)">播放</el-button><el-button v-else-if="['pending','failed'].includes(row.status)" type="primary" link @click.stop="scheduleResource(row)">{{row.status==='failed'?'重新下载':'加入队列'}}</el-button><el-button v-if="row.download_task_id" type="danger" link @click.stop="removeResource(row)">删除文件</el-button></template></el-table-column>
          </el-table>
          <div class="pagination"><el-pagination v-model:current-page="store.resourceQuery.page" v-model:page-size="store.resourceQuery.page_size" :page-sizes="[20,50,100]" layout="total, sizes, prev, pager, next, jumper" :total="store.resourceTotal" @current-change="changeResourcePage" @size-change="changeResourceSize"/></div>
        </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>

  <el-dialog v-model="formVisible" :title="editing ? '编辑采集任务' : '新建采集任务'" width="560px">
    <el-form label-position="top"><el-form-item label="任务名称"><el-input v-model="form.name" placeholder="可选，例如：123AV 今日 FC2"/></el-form-item><el-form-item label="列表链接" required><el-input v-model="form.url" placeholder="https://123av.com/cn/makers/fc2?sort=today&page=1"/></el-form-item><el-form-item label="每天定时采集"><div class="schedule-field"><el-switch v-model="form.scheduled_enabled"/><span>开启后每天北京时间 18:00 自动执行</span></div></el-form-item></el-form>
    <template #footer><el-button @click="formVisible=false">取消</el-button><el-button type="primary" @click="saveCollection">保存</el-button></template>
  </el-dialog>
  <el-dialog v-model="keyVisible" title="API Key" width="440px"><el-input v-model="keyValue" type="password" show-password placeholder="未配置可留空"/><template #footer><el-button @click="keyVisible=false">取消</el-button><el-button type="primary" @click="saveKey">保存</el-button></template></el-dialog>
  <el-dialog v-model="playerVisible" :title="playing?.title || playing?.video_id || '视频播放'" width="min(1000px, 92vw)" destroy-on-close class="video-dialog"><VideoPlayer v-if="playing && playerVisible" :task="playing"/></el-dialog>
</template>
