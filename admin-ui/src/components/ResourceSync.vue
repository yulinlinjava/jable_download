<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'

interface SyncSite {
  id: string; name: string; enabled: boolean; endpoint_url: string; auth_type: string
  header_name: string; has_secret: boolean; resolve_media?: boolean; batch_size: number; timeout: number
  created_at: number; updated_at: number
}

const visible = defineModel<boolean>({ default: false })
const props = defineProps<{ ids: string[] }>()
const emit = defineEmits<{ synced: [] }>()
const loading = ref(false), saving = ref(false), syncing = ref(false)
const sites = ref<SyncSite[]>([]), runs = ref<any[]>([])
const selectedSiteId = ref(''), editorVisible = ref(false), editingId = ref('')
const form = reactive({ name: '', enabled: true, endpoint_url: '', auth_type: 'none', header_name: 'X-API-Key', secret: '', has_secret: false, batch_size: 50, timeout: 30 })
const regularSites = computed(() => sites.value.filter(site => !site.resolve_media))
const enabledSites = computed(() => regularSites.value.filter(site => site.enabled))
const visibleRuns = computed(() => {
  const siteIds = new Set(regularSites.value.map(site => site.id))
  return runs.value.filter(run => siteIds.has(run.site_id))
})
const formatTime = (value?: number) => value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'
const statusText = (value: string) => ({ completed: '成功', failed: '失败', running: '同步中' }[value] || value)
const statusType = (value: string) => value === 'completed' ? 'success' : value === 'failed' ? 'danger' : 'primary'
const authText = (value: string) => ({ none: '无鉴权', bearer: 'Bearer Token', api_key: 'API Key' }[value] || value)

async function load() {
  loading.value = true
  try {
    const [siteResult, history] = await Promise.all([api.get('/resource-sync/sites'), api.get('/resource-sync/runs')])
    sites.value = siteResult.data.data
    runs.value = history.data.data
    if (!enabledSites.value.some(site => site.id === selectedSiteId.value)) selectedSiteId.value = enabledSites.value[0]?.id || ''
  } catch (e: any) { ElMessage.error(e.message) } finally { loading.value = false }
}
function createSite() {
  editingId.value = ''
  Object.assign(form, { name: '', enabled: true, endpoint_url: '', auth_type: 'none', header_name: 'X-API-Key', secret: '', has_secret: false, batch_size: 50, timeout: 30 })
  editorVisible.value = true
}
function editSite(row: SyncSite) {
  editingId.value = row.id
  Object.assign(form, row, { secret: '' })
  editorVisible.value = true
}
async function saveSite() {
  if (!form.name.trim()) return ElMessage.warning('请输入网站名称')
  if (!form.endpoint_url.trim()) return ElMessage.warning('请输入同步接口地址')
  saving.value = true
  try {
    const data = { name: form.name.trim(), enabled: form.enabled, endpoint_url: form.endpoint_url.trim(), auth_type: form.auth_type, header_name: form.header_name, secret: form.secret || null, resolve_media: false, batch_size: form.batch_size, timeout: form.timeout }
    editingId.value ? await api.put(`/resource-sync/sites/${editingId.value}`, data) : await api.post('/resource-sync/sites', data)
    ElMessage.success(editingId.value ? '网站已更新' : '网站已添加')
    editorVisible.value = false
    await load()
  } catch (e: any) { ElMessage.error(e.message) } finally { saving.value = false }
}
async function removeSite(row: SyncSite) {
  await ElMessageBox.confirm(`删除第三方网站“${row.name}”？历史同步记录仍会保留。`, '删除确认', { type: 'warning' })
  try {
    await api.delete(`/resource-sync/sites/${row.id}`)
    ElMessage.success('网站已删除')
    await load()
  } catch (e: any) { ElMessage.error(e.message) }
}
async function syncResources(all = false) {
  if (!selectedSiteId.value) return ElMessage.warning('请先选择一个已启用的网站')
  if (!all && !props.ids.length) return ElMessage.warning('请先在资源列表选择资源')
  if (all) await ElMessageBox.confirm('将把资源管理中的全部资源同步到所选网站，是否继续？', '全部同步', { type: 'warning' })
  syncing.value = true
  try {
    const run = (await api.post('/resource-sync/execute', { site_id: selectedSiteId.value, ids: all ? [] : props.ids, all }, { timeout: 0 })).data.data
    run.failed ? ElMessage.warning(`同步完成：成功 ${run.succeeded} 条，失败 ${run.failed} 条`) : ElMessage.success(`已成功同步 ${run.succeeded} 条资源`)
    emit('synced'); await load()
  } catch (e: any) { ElMessage.error(e.message) } finally { syncing.value = false }
}
async function retry(row: any) {
  syncing.value = true
  try {
    const run = (await api.post(`/resource-sync/runs/${row.id}/retry`, {}, { timeout: 0 })).data.data
    run.failed ? ElMessage.warning(`重试完成：仍有 ${run.failed} 条失败`) : ElMessage.success('失败资源已同步成功')
    emit('synced'); await load()
  } catch (e: any) { ElMessage.error(e.message) } finally { syncing.value = false }
}
watch(visible, value => { if (value) load() })
</script>

<template>
  <el-dialog v-model="visible" title="发送数据" width="min(1100px, 96vw)" :close-on-click-modal="false">
    <div v-loading="loading">
      <el-alert type="info" :closable="false" show-icon title="仅发送已经下载完成的资源；“全部同步”也会自动排除待下载、下载中和失败资源。"/>
      <div class="site-toolbar">
        <div><strong>同步网站</strong><div class="secondary-cell">维护多个接收资源的第三方接口，同步时选择目标网站。</div></div>
        <el-button type="primary" @click="createSite">添加网站</el-button>
      </div>
      <el-table :data="regularSites" max-height="260" empty-text="还没有第三方网站，请先添加">
        <el-table-column prop="name" label="网站名称" min-width="130"/>
        <el-table-column label="状态" width="85"><template #default="{row}"><el-tag :type="row.enabled?'success':'info'">{{row.enabled?'已启用':'已停用'}}</el-tag></template></el-table-column>
        <el-table-column prop="endpoint_url" label="同步接口" min-width="280" show-overflow-tooltip/>
        <el-table-column label="鉴权" width="120"><template #default="{row}">{{authText(row.auth_type)}}</template></el-table-column>
        <el-table-column label="批量 / 超时" width="125"><template #default="{row}">{{row.batch_size}} 条 / {{row.timeout}} 秒</template></el-table-column>
        <el-table-column label="操作" width="120" fixed="right"><template #default="{row}"><el-button link type="primary" @click="editSite(row)">编辑</el-button><el-button link type="danger" @click="removeSite(row)">删除</el-button></template></el-table-column>
      </el-table>

      <el-divider content-position="left">执行同步</el-divider>
      <div class="sync-runbar">
        <el-select v-model="selectedSiteId" filterable placeholder="选择目标网站" style="width:280px">
          <el-option v-for="site in enabledSites" :key="site.id" :label="site.name" :value="site.id"/>
        </el-select>
        <el-button type="primary" :loading="syncing" :disabled="!selectedSiteId || !props.ids.length" @click="syncResources(false)">同步选中资源（{{props.ids.length}}）</el-button>
        <el-button type="primary" plain :loading="syncing" :disabled="!selectedSiteId" @click="syncResources(true)">同步全部资源</el-button>
        <span v-if="!enabledSites.length" class="warning-text">请添加并启用至少一个网站</span>
      </div>

      <el-divider content-position="left">最近同步记录</el-divider>
      <el-table :data="visibleRuns" max-height="300" empty-text="暂无同步记录">
        <el-table-column label="时间" width="175"><template #default="{row}">{{formatTime(row.created)}}</template></el-table-column>
        <el-table-column prop="site_name" label="目标网站" min-width="130"><template #default="{row}">{{row.site_name || '历史配置'}}</template></el-table-column>
        <el-table-column label="范围" width="90"><template #default="{row}">{{row.scope==='all'?'全部':'已选择'}}</template></el-table-column>
        <el-table-column label="状态" width="90"><template #default="{row}"><el-tag :type="statusType(row.status)">{{statusText(row.status)}}</el-tag></template></el-table-column>
        <el-table-column prop="total" label="总数" width="70"/><el-table-column prop="succeeded" label="成功" width="70"/><el-table-column prop="failed" label="失败" width="70"/>
        <el-table-column prop="error" label="结果" min-width="150"><template #default="{row}">{{row.error || '同步完成'}}</template></el-table-column>
        <el-table-column label="操作" width="70"><template #default="{row}"><el-button v-if="row.failed" link type="primary" :disabled="syncing || !row.site_id" @click="retry(row)">重试</el-button></template></el-table-column>
      </el-table>
    </div>
    <template #footer><el-button :disabled="saving || syncing" @click="visible=false">关闭</el-button></template>
  </el-dialog>

  <el-dialog v-model="editorVisible" :title="editingId ? '编辑第三方网站' : '添加第三方网站'" width="min(680px, 94vw)" append-to-body :close-on-click-modal="false">
    <el-form label-position="top" class="sync-form">
      <el-form-item label="网站名称" class="wide" required><el-input v-model="form.name" placeholder="例如：内容站 A"/></el-form-item>
      <el-form-item label="启用网站"><el-switch v-model="form.enabled"/></el-form-item>
      <el-form-item label="鉴权方式"><el-select v-model="form.auth_type"><el-option label="无鉴权" value="none"/><el-option label="Bearer Token" value="bearer"/><el-option label="API Key" value="api_key"/></el-select></el-form-item>
      <el-form-item label="同步接口地址" class="wide" required><el-input v-model="form.endpoint_url" placeholder="https://api.example.com/resources/import"/></el-form-item>
      <el-form-item v-if="form.auth_type==='api_key'" label="API Key 请求头"><el-input v-model="form.header_name"/></el-form-item>
      <el-form-item v-if="form.auth_type!=='none'" :label="form.has_secret ? '密钥（留空保持原值）' : '密钥'"><el-input v-model="form.secret" type="password" show-password autocomplete="new-password"/></el-form-item>
      <el-form-item label="每批数量"><el-input-number v-model="form.batch_size" :min="1" :max="100"/></el-form-item>
      <el-form-item label="超时（秒）"><el-input-number v-model="form.timeout" :min="5" :max="120"/></el-form-item>
    </el-form>
    <template #footer><el-button :disabled="saving" @click="editorVisible=false">取消</el-button><el-button type="primary" :loading="saving" @click="saveSite">保存</el-button></template>
  </el-dialog>
</template>
