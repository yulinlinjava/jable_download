<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'

interface DownloadNode {
  id: string
  name: string
  enabled: boolean
  endpoint_url: string
  header_name: string
  has_secret: boolean
  resolve_media: boolean
  batch_size: number
  timeout: number
  updated_at: number
}

const loading = ref(false)
const saving = ref(false)
const nodes = ref<DownloadNode[]>([])
const editorVisible = ref(false)
const editingId = ref('')
const form = reactive({
  name: '', enabled: true, endpoint_url: '', header_name: 'X-API-Key',
  secret: '', has_secret: false, batch_size: 50, timeout: 60,
})
const formatTime = (value?: number) => value
  ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'

async function load() {
  loading.value = true
  try {
    const result = await api.get('/resource-sync/sites')
    nodes.value = result.data.data.filter((item: DownloadNode) => item.resolve_media)
  } catch (error: any) {
    ElMessage.error(error.message)
  } finally {
    loading.value = false
  }
}

function createNode() {
  editingId.value = ''
  Object.assign(form, {
    name: '', enabled: true, endpoint_url: '', header_name: 'X-API-Key',
    secret: '', has_secret: false, batch_size: 50, timeout: 60,
  })
  editorVisible.value = true
}

function editNode(row: DownloadNode) {
  editingId.value = row.id
  Object.assign(form, row, { secret: '' })
  editorVisible.value = true
}

async function saveNode() {
  if (!form.name.trim()) return ElMessage.warning('请输入节点名称')
  if (!form.endpoint_url.trim()) return ElMessage.warning('请输入节点地址')
  if (!form.secret && !form.has_secret) return ElMessage.warning('请输入节点 API Key')
  saving.value = true
  try {
    const data = {
      name: form.name.trim(), enabled: form.enabled,
      endpoint_url: form.endpoint_url.trim(), auth_type: 'api_key',
      header_name: form.header_name.trim() || 'X-API-Key',
      secret: form.secret || null, resolve_media: true,
      batch_size: form.batch_size, timeout: form.timeout,
    }
    if (editingId.value) await api.put(`/resource-sync/sites/${editingId.value}`, data)
    else await api.post('/resource-sync/sites', data)
    ElMessage.success(editingId.value ? '下载节点已更新' : '下载节点已添加')
    editorVisible.value = false
    await load()
  } catch (error: any) {
    ElMessage.error(error.message)
  } finally {
    saving.value = false
  }
}

async function removeNode(row: DownloadNode) {
  try {
    await ElMessageBox.confirm(`删除下载节点“${row.name}”？`, '删除确认', { type: 'warning' })
    await api.delete(`/resource-sync/sites/${row.id}`)
    ElMessage.success('下载节点已删除')
    await load()
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || '删除失败')
  }
}

load()
</script>

<template>
  <el-card shadow="never" class="node-panel">
    <template #header>
      <div class="node-head">
        <div>
          <h2>下载节点</h2>
          <p>配置远程 JableTVDownload 服务。资源逐条解析成功后立即调用节点的 JSON 导入接口。</p>
        </div>
        <el-button type="primary" @click="createNode">添加节点</el-button>
      </div>
    </template>

    <el-alert type="info" :closable="false" show-icon>
      <template #title>节点地址可填 <code>http://43.225.196.162:9178</code>，系统会自动补全 <code>/api/collection-items/import</code>。API Key 填写节点的 <code>JABLE_API_KEY</code>。</template>
    </el-alert>

    <el-table v-loading="loading" :data="nodes" class="node-table" empty-text="还没有下载节点">
      <el-table-column prop="name" label="节点名称" min-width="150"/>
      <el-table-column label="状态" width="95"><template #default="{row}"><el-tag :type="row.enabled?'success':'info'">{{row.enabled?'已启用':'已停用'}}</el-tag></template></el-table-column>
      <el-table-column prop="endpoint_url" label="节点地址 / JSON 导入接口" min-width="360" show-overflow-tooltip/>
      <el-table-column prop="header_name" label="鉴权请求头" width="130"/>
      <el-table-column label="API Key" width="95"><template #default="{row}"><el-tag :type="row.has_secret?'success':'danger'" effect="plain">{{row.has_secret?'已配置':'未配置'}}</el-tag></template></el-table-column>
      <el-table-column label="请求超时" width="110"><template #default="{row}">{{row.timeout}} 秒</template></el-table-column>
      <el-table-column label="更新时间" width="175"><template #default="{row}">{{formatTime(row.updated_at)}}</template></el-table-column>
      <el-table-column label="操作" width="120" fixed="right"><template #default="{row}"><el-button link type="primary" @click="editNode(row)">编辑</el-button><el-button link type="danger" @click="removeNode(row)">删除</el-button></template></el-table-column>
    </el-table>
  </el-card>

  <el-dialog v-model="editorVisible" :title="editingId ? '编辑下载节点' : '添加下载节点'" width="min(680px, 94vw)" :close-on-click-modal="false">
    <el-form label-position="top" class="node-form">
      <el-form-item label="节点名称" required><el-input v-model="form.name" placeholder="例如：美国下载节点"/></el-form-item>
      <el-form-item label="启用节点"><el-switch v-model="form.enabled"/></el-form-item>
      <el-form-item label="节点地址" required class="wide"><el-input v-model="form.endpoint_url" placeholder="http://server:8000"/><div class="form-tip">可填节点根地址或完整的 <code>/api/collection-items/import</code> 接口。</div></el-form-item>
      <el-form-item label="API Key 请求头"><el-input v-model="form.header_name" placeholder="X-API-Key"/></el-form-item>
      <el-form-item :label="form.has_secret ? 'API Key（留空保持原值）' : 'API Key'" required><el-input v-model="form.secret" type="password" show-password autocomplete="new-password"/></el-form-item>
      <el-form-item label="请求超时（秒）"><el-input-number v-model="form.timeout" :min="5" :max="120"/></el-form-item>
    </el-form>
    <template #footer><el-button :disabled="saving" @click="editorVisible=false">取消</el-button><el-button type="primary" :loading="saving" @click="saveNode">保存</el-button></template>
  </el-dialog>
</template>

<style scoped>
.node-panel { border: 0; border-radius: 16px; }
.node-head { display: flex; align-items: center; justify-content: space-between; gap: 20px; }
.node-head h2 { margin: 0 0 5px; font-size: 18px; }
.node-head p { margin: 0; color: #64748b; }
.node-table { margin-top: 18px; }
.node-form { display: grid; grid-template-columns: 1fr 1fr; gap: 0 18px; }
.node-form .wide { grid-column: 1 / -1; }
.form-tip { margin-top: 6px; color: #64748b; font-size: 12px; }
code { padding: 2px 5px; border-radius: 4px; background: #eef2f7; }
@media (max-width: 720px) { .node-form { display: block; } .node-head { align-items: flex-start; } }
</style>
