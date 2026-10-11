<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const visible = defineModel<boolean>({ default: false })
const emit = defineEmits<{ imported: []; manageSignatures: [] }>()
const input = ref(''), busy = ref(false)
const error = ref(''), previewText = ref(''), previewItems = ref<any[]>()
interface ImportResult {
  total: number; created: number; updated: number; skipped: number; failed: number; queued: number; local_linked: number; saved_only?: number
  results: { index: number; status: string; message: string }[]
}
interface PreviewResult {
  total: number; valid: number; updated: number; skipped: number; failed: number; queued: number; local_linked: number; saved_only?: number
  results: { index: number; status: string; message: string }[]
}
const result = ref<ImportResult>(), validation = ref<PreviewResult>()
function clearPreview() { previewText.value = ''; previewItems.value = undefined; validation.value = undefined; result.value = undefined }
function invalidatePreview() { error.value = ''; clearPreview() }
function example() {
  input.value = JSON.stringify([{source:'external',id:'demo-001',title:'示例资源一',
    cover:'https://cdn.example.com/demo-001/cover.jpg',m3u8_url:'https://cdn.example.com/demo-001/index.m3u8'}], null, 2)
  invalidatePreview()
}
function parseInput() {
  try { return JSON.parse(input.value) }
  catch (e: any) { throw new Error(`JSON 格式错误：${e.message}`) }
}
async function preview() {
  error.value = ''; result.value = undefined; busy.value = true
  try {
    const checked = (await api.post('/collection-items/import/validate', parseInput())).data.data
    previewItems.value = checked.items; previewText.value = JSON.stringify(checked.items, null, 2); validation.value = checked
  } catch (e: any) { error.value = e.message }
  finally { busy.value = false }
}
async function submit() {
  error.value = ''; busy.value = true
  try {
    // Always submit original input; the import endpoint owns source matching and formatting.
    result.value = (await api.post('/collection-items/import', parseInput())).data.data
    ElMessage.success(`导入完成：新增 ${result.value?.created || 0} 条，覆盖 ${result.value?.updated || 0} 条`)
    emit('imported')
  } catch (e: any) { error.value = e.message }
  finally { busy.value = false }
}
function manageSignatures() { visible.value = false; emit('manageSignatures') }
watch(visible, value => { if(value) invalidatePreview() })
watch(input, invalidatePreview)
</script>

<template>
  <el-dialog v-model="visible" title="导入数据" width="min(1040px, 96vw)" :close-on-click-modal="false"
    :close-on-press-escape="!busy" :show-close="!busy">
    <p>粘贴 JSON，系统根据数据中的 source 自动匹配来源管理里的格式化脚本；未配置脚本的来源直接校验标准数据。链接在预览或下载时签名。</p>
    <div class="import-mode-row">
      <el-button :disabled="busy || !!input.trim()" @click="example">填入示例</el-button>
      <el-button :disabled="busy" @click="manageSignatures">来源管理</el-button>
    </div>
    <el-input v-model="input" type="textarea" :rows="12" :disabled="busy" spellcheck="false"
      placeholder="资源数组中的每条数据包含 source；原始对象可在顶层提供 source，由对应脚本解析"/>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon/>

    <template v-if="previewItems">
      <div class="import-preview-heading"><strong>处理结果预览</strong><span>{{ previewItems.length }} 条资源</span></div>
      <el-input :model-value="previewText" type="textarea" :rows="10" readonly spellcheck="false"/>
      <el-alert v-if="validation" :type="validation.failed ? 'warning' : 'success'" :closable="false"
        :title="`校验 ${validation.total} 条：有效 ${validation.valid} 条，覆盖 ${validation.updated || 0} 条，本地关联 ${validation.local_linked} 条，远程排队 ${validation.queued} 条，仅保存 ${validation.saved_only || 0} 条，跳过 ${validation.skipped} 条，失败 ${validation.failed} 条`"/>
      <el-table v-if="validation && (validation.failed || validation.skipped)"
        :data="validation.results.filter(item => item.status !== 'valid')" max-height="180">
        <el-table-column prop="index" label="数组第几条" width="110"/>
        <el-table-column label="结果" width="80"><template #default="{ row }">{{ row.status === 'failed' ? '失败' : '跳过' }}</template></el-table-column>
        <el-table-column prop="message" label="原因"/>
      </el-table>
    </template>

    <template v-if="result">
      <el-alert :type="result.failed ? 'warning' : 'success'" :closable="false"
        :title="`共 ${result.total} 条：新增 ${result.created} 条，覆盖 ${result.updated || 0} 条，本地关联 ${result.local_linked} 条，加入下载队列 ${result.queued} 条，仅保存 ${result.saved_only || 0} 条，跳过 ${result.skipped} 条，失败 ${result.failed} 条`"/>
      <el-table :data="result.results.filter(item => !['created','updated'].includes(item.status))" max-height="180" empty-text="全部导入成功">
        <el-table-column prop="index" label="数组第几条" width="110"/>
        <el-table-column label="结果" width="80"><template #default="{ row }">{{ row.status === 'failed' ? '失败' : '跳过' }}</template></el-table-column>
        <el-table-column prop="message" label="原因"/>
      </el-table>
    </template>
    <template #footer>
      <el-button :disabled="busy" @click="visible=false">关闭</el-button>
      <el-button :loading="busy" :disabled="!input.trim()" @click="preview">预览数据</el-button>
      <el-button type="primary" :loading="busy" :disabled="!input.trim()" @click="submit">确认导入</el-button>
    </template>
  </el-dialog>
</template>
