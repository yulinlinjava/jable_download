<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, type ImportScript } from '../api'
import ImportScriptManager from './ImportScriptManager.vue'

const visible = defineModel<boolean>({ default: false })
const emit = defineEmits<{ imported: [] }>()
const input = ref(''), mode = ref<'direct' | 'script'>('direct'), scriptId = ref('')
const scripts = ref<ImportScript[]>([]), busy = ref(false), loadingScripts = ref(false)
const error = ref(''), previewText = ref(''), previewItems = ref<any[]>()
const managerVisible = ref(false)
interface ImportResult {
  total: number; created: number; skipped: number; failed: number; queued: number; local_linked: number
  results: { index: number; status: string; message: string }[]
}
interface PreviewResult {
  total: number; valid: number; skipped: number; failed: number; queued: number; local_linked: number
  results: { index: number; status: string; message: string }[]
}
const result = ref<ImportResult>(), validation = ref<PreviewResult>()
const selectedScript = computed(() => scripts.value.find((item) => item.id === scriptId.value))

function clearPreview() {
  previewText.value = ''; previewItems.value = undefined; validation.value = undefined; result.value = undefined
}
function invalidatePreview() { error.value = ''; clearPreview() }
function example() {
  input.value = JSON.stringify([{
    source: 'external', id: 'demo-001', title: '示例资源一',
    cover: 'https://cdn.example.com/demo-001/cover.jpg',
    duration: '01:30:00', published_at: '2026-09-18', actors: ['演员甲'], tags: ['标签一'],
    m3u8_url: 'https://cdn.example.com/demo-001/index.m3u8',
  }], null, 2)
  error.value = ''; clearPreview()
}
async function loadScripts() {
  loadingScripts.value = true
  try {
    scripts.value = (await api.get('/import-scripts')).data.data
    if (!scripts.value.some((item) => item.id === scriptId.value)) scriptId.value = scripts.value[0]?.id || ''
  } catch (e: any) { error.value = e.message }
  finally { loadingScripts.value = false }
}
function parseInput() {
  try { return JSON.parse(input.value) }
  catch (e: any) { throw new Error(`JSON 格式错误：${e.message}`) }
}
function ensureItems(value: unknown): any[] {
  if (!Array.isArray(value) || !value.length || value.length > 1000) {
    throw new Error('处理结果必须是包含 1–1000 条资源的数组')
  }
  return value
}
function executeFormatter(code: string, raw: unknown): Promise<unknown> {
  const workerSource = `
    const encoder = new TextEncoder();
    const bytes = (value) => typeof value === 'string' ? encoder.encode(value) : new Uint8Array(value);
    const hex = (value) => Array.from(new Uint8Array(value), b => b.toString(16).padStart(2, '0')).join('');
    const subtle = () => {
      if (!self.crypto || !self.crypto.subtle) throw new Error('SHA/HMAC 助手需要通过 HTTPS 打开管理后台');
      return self.crypto.subtle;
    };
    const helpers = {
      now: () => Math.floor(Date.now() / 1000),
      sha256: async (value) => hex(await subtle().digest('SHA-256', bytes(value))),
      hmacSha256: async (secret, value) => {
        const key = await subtle().importKey('raw', bytes(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
        return hex(await subtle().sign('HMAC', key, bytes(value)));
      },
      base64Url: (value) => {
        let binary = ''; for (const item of bytes(value)) binary += String.fromCharCode(item);
        return btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
      }
    };
    self.onmessage = async ({ data }) => {
      try {
        const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
        const runner = new AsyncFunction('raw', 'helpers', data.code + '\\n;if (typeof format !== "function") throw new Error("脚本必须声明 format(raw, helpers) 函数"); return await format(raw, helpers);');
        const result = await runner(data.raw, helpers);
        self.postMessage({ ok: true, result });
      } catch (error) {
        self.postMessage({ ok: false, error: error && error.message ? error.message : String(error) });
      }
    }`
  return new Promise((resolve, reject) => {
    const blobUrl = URL.createObjectURL(new Blob([workerSource], { type: 'text/javascript' }))
    const worker = new Worker(blobUrl)
    const finish = () => { worker.terminate(); URL.revokeObjectURL(blobUrl); window.clearTimeout(timer) }
    const timer = window.setTimeout(() => { finish(); reject(new Error('格式化脚本执行超过 30 秒，已停止')) }, 30000)
    worker.onmessage = ({ data }) => {
      finish()
      if (data.ok) resolve(data.result); else reject(new Error(`格式化脚本错误：${data.error}`))
    }
    worker.onerror = (event) => { finish(); reject(new Error(`格式化脚本错误：${event.message}`)) }
    worker.postMessage({ code, raw })
  })
}
async function buildItems() {
  const raw = parseInput()
  if (mode.value === 'direct') return ensureItems(raw)
  if (!selectedScript.value) throw new Error('请选择格式化脚本')
  return ensureItems(await executeFormatter(selectedScript.value.code, raw))
}
async function preview() {
  error.value = ''; result.value = undefined; busy.value = true
  try {
    const items = await buildItems()
    const checked = (await api.post('/collection-items/import/validate', items)).data.data
    previewItems.value = items; previewText.value = JSON.stringify(items, null, 2); validation.value = checked
  } catch (e: any) { clearPreview(); error.value = e.message }
  finally { busy.value = false }
}
async function submit() {
  error.value = ''; result.value = undefined; busy.value = true
  try {
    const items = mode.value === 'script'
      ? (previewItems.value || (() => { throw new Error('请先执行格式化并预览') })())
      : ensureItems(parseInput())
    result.value = (await api.post('/collection-items/import', items)).data.data
    emit('imported')
    if (result.value?.created || result.value?.updated) {
      ElMessage.success(`导入完成：新增 ${result.value.created || 0} 条，覆盖 ${result.value.updated || 0} 条`)
    }
  } catch (e: any) { error.value = e.message }
  finally { busy.value = false }
}
async function scriptsChanged() { clearPreview(); await loadScripts() }
watch(visible, (value) => { if (value) loadScripts() })
watch([input, mode, scriptId], invalidatePreview)
</script>

<template>
  <el-dialog v-model="visible" title="导入数据" width="min(1040px, 96vw)" :close-on-click-modal="false"
    :close-on-press-escape="!busy" :show-close="!busy">
    <p>粘贴原始 JSON。可以直接导入标准资源数组，也可以选择脚本完成字段转换和播放链接签名。</p>
    <div class="import-mode-row">
      <el-radio-group v-model="mode" :disabled="busy">
        <el-radio-button value="direct">原始数据直接导入</el-radio-button>
        <el-radio-button value="script">使用格式化脚本</el-radio-button>
      </el-radio-group>
      <template v-if="mode === 'script'">
        <el-select v-model="scriptId" :loading="loadingScripts" placeholder="选择格式化脚本" style="width:280px">
          <el-option v-for="item in scripts" :key="item.id" :label="item.name" :value="item.id">
            <span>{{ item.name }}</span><span class="script-option-desc">{{ item.description }}</span>
          </el-option>
        </el-select>
        <el-button @click="managerVisible=true">管理脚本</el-button>
      </template>
      <el-button v-else :disabled="busy || !!input.trim()" @click="example">填入示例</el-button>
    </div>
    <p v-if="mode === 'script' && selectedScript?.description" class="secondary-cell">{{ selectedScript.description }}</p>
    <el-input v-model="input" type="textarea" :rows="12" :disabled="busy" spellcheck="false"
      placeholder="粘贴原始 JSON；使用脚本时可以是数组或任意 JSON 对象"/>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon/>

    <template v-if="previewItems">
      <div class="import-preview-heading"><strong>处理结果预览</strong><span>{{ previewItems.length }} 条资源</span></div>
      <el-input :model-value="previewText" type="textarea" :rows="10" readonly spellcheck="false"/>
      <el-alert v-if="validation" :type="validation.failed ? 'warning' : 'success'" :closable="false"
        :title="`校验 ${validation.total} 条：有效 ${validation.valid} 条，覆盖 ${validation.updated || 0} 条，本地关联 ${validation.local_linked} 条，远程排队 ${validation.queued} 条，跳过 ${validation.skipped} 条，失败 ${validation.failed} 条`"/>
      <el-table v-if="validation && (validation.failed || validation.skipped)"
        :data="validation.results.filter(item => item.status !== 'valid')" max-height="180">
        <el-table-column prop="index" label="数组第几条" width="110"/>
        <el-table-column label="结果" width="80"><template #default="{ row }">{{ row.status === 'failed' ? '失败' : '跳过' }}</template></el-table-column>
        <el-table-column prop="message" label="原因"/>
      </el-table>
    </template>

    <template v-if="result">
      <el-alert :type="result.failed ? 'warning' : 'success'" :closable="false"
        :title="`共 ${result.total} 条：新增 ${result.created} 条，覆盖 ${result.updated || 0} 条，本地关联 ${result.local_linked} 条，加入下载队列 ${result.queued} 条，跳过 ${result.skipped} 条，失败 ${result.failed} 条`"/>
      <el-table :data="result.results.filter(item => !['created','updated'].includes(item.status))" max-height="180" empty-text="全部导入成功">
        <el-table-column prop="index" label="数组第几条" width="110"/>
        <el-table-column label="结果" width="80"><template #default="{ row }">{{ row.status === 'failed' ? '失败' : '跳过' }}</template></el-table-column>
        <el-table-column prop="message" label="原因"/>
      </el-table>
    </template>
    <template #footer>
      <el-button :disabled="busy" @click="visible=false">关闭</el-button>
      <el-button :loading="busy" :disabled="!input.trim()" @click="preview">{{ mode === 'script' ? '格式化并预览' : '校验并预览' }}</el-button>
      <el-button type="primary" :loading="busy" :disabled="!input.trim() || (mode === 'script' && !previewItems)" @click="submit">确认导入</el-button>
    </template>
  </el-dialog>
  <ImportScriptManager v-model="managerVisible" @changed="scriptsChanged"/>
</template>
