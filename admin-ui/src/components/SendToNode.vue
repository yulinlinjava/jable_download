<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'

interface DownloadNode {
  id: string
  name: string
  enabled: boolean
  resolve_media: boolean
}

const visible = defineModel<boolean>({ default: false })
const props = defineProps<{ ids: string[] }>()
const emit = defineEmits<{ sent: [] }>()
const loading = ref(false)
const sending = ref(false)
const nodes = ref<DownloadNode[]>([])
const selectedNodeId = ref('')
const enabledNodes = computed(() => nodes.value.filter(node => node.enabled))
async function load() {
  loading.value = true
  try {
    const siteResult = await api.get('/resource-sync/sites')
    nodes.value = siteResult.data.data.filter((item: DownloadNode) => item.resolve_media)
    if (!enabledNodes.value.some(node => node.id === selectedNodeId.value)) {
      selectedNodeId.value = enabledNodes.value[0]?.id || ''
    }
  } catch (error: any) {
    ElMessage.error(error.message)
  } finally {
    loading.value = false
  }
}

async function send(all: boolean) {
  if (!selectedNodeId.value) return ElMessage.warning('请先选择已启用的下载节点')
  if (!all && !props.ids.length) return ElMessage.warning('请先在资源列表勾选资源')
  if (all) {
    try {
      await ElMessageBox.confirm('将检查全部资源的发送记录，仅解析并发送该节点尚未接收或此前发送失败的资源，是否继续？', '发送全部资源', { type: 'warning' })
    } catch (error) {
      return
    }
  }
  sending.value = true
  try {
    const response = await api.post('/resource-sync/execute', {
      site_id: selectedNodeId.value, ids: all ? [] : props.ids, all,
    }, { timeout: 0 })
    const run = response.data.data
    const skipped = Number(run.skipped || 0)
    const queued = Number(run.total || 0)
    if (!queued && skipped) ElMessage.info(`所选资源均已调度到该节点，已跳过 ${skipped} 条`)
    else ElMessage.success(`已加入等待发送 ${queued} 条，已发送跳过 ${skipped} 条`)
    emit('sent')
    window.dispatchEvent(new CustomEvent('resource-sync-records-changed'))
  } catch (error: any) {
    ElMessage.error(error.message)
  } finally {
    sending.value = false
  }
}

watch(visible, value => { if (value) load() })
</script>

<template>
  <el-dialog v-model="visible" title="调度至下载节点" width="min(920px, 96vw)" :close-on-click-modal="false">
    <div v-loading="loading">
      <el-alert type="info" :closable="false" show-icon title="系统先按节点和资源番号跳过已发送数据，只为未发送或发送失败的资源解析播放地址，再交给节点排队下载。"/>
      <div class="send-bar">
        <el-select v-model="selectedNodeId" filterable placeholder="选择下载节点" class="node-select">
          <el-option v-for="node in enabledNodes" :key="node.id" :label="node.name" :value="node.id"/>
        </el-select>
        <el-button type="primary" :loading="sending" :disabled="!selectedNodeId || !props.ids.length" @click="send(false)">发送选中资源（{{props.ids.length}}）</el-button>
        <el-button type="primary" plain :loading="sending" :disabled="!selectedNodeId" @click="send(true)">发送全部资源</el-button>
      </div>
      <div v-if="!enabledNodes.length" class="empty-tip">请先在“下载节点”Tab 添加并启用节点。</div>
      <div class="record-tip">发送结果、搜索、删除和重试请在“发送记录”Tab 中操作。</div>
    </div>
    <template #footer><el-button :disabled="sending" @click="visible=false">关闭</el-button></template>
  </el-dialog>
</template>

<style scoped>
.send-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin: 18px 0 8px; }
.node-select { width: 280px; }
.empty-tip { margin-top: 12px; color: #b45309; }
.record-tip { margin-top: 14px; color: #64748b; font-size: 13px; }
</style>
