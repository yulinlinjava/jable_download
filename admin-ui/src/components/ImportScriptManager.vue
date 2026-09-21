<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type ImportScript } from '../api'

const visible = defineModel<boolean>({ default: false })
const emit = defineEmits<{ changed: [] }>()
const rows = ref<ImportScript[]>([]), loading = ref(false), saving = ref(false), editorVisible = ref(false)
const form = reactive({ id: '', name: '', description: '', code: '' })
const starter = `async function format(raw, helpers) {
  const items = Array.isArray(raw) ? raw : (raw.data?.list || raw.items || []);

  return items.map((item) => ({
    source: item.source || 'external',
    id: item.id || item.code,
    title: String(item.title || item.name || '').trim(),
    url: item.url || null,
    m3u8_url: item.m3u8_url || item.videoUrl || item.playUrl,
    cover: item.cover || item.coverUrl || null,
    actors: Array.isArray(item.actors) ? item.actors : [],
    tags: Array.isArray(item.tags) ? item.tags : []
  }));
}`

async function load() {
  loading.value = true
  try { rows.value = (await api.get('/import-scripts')).data.data }
  catch (error: any) { ElMessage.error(error.message) }
  finally { loading.value = false }
}
function edit(row?: ImportScript) {
  Object.assign(form, row
    ? { id: row.id, name: row.name, description: row.description, code: row.code }
    : { id: '', name: '', description: '', code: starter })
  editorVisible.value = true
}
async function save() {
  if (!form.name.trim() || !form.code.trim()) { ElMessage.warning('请填写脚本名称和代码'); return }
  saving.value = true
  try {
    const body = { name: form.name.trim(), description: form.description.trim(), code: form.code }
    if (form.id) await api.put(`/import-scripts/${form.id}`, body)
    else await api.post('/import-scripts', body)
    ElMessage.success(form.id ? '脚本已保存' : '脚本已创建')
    editorVisible.value = false
    await load(); emit('changed')
  } catch (error: any) { ElMessage.error(error.message) }
  finally { saving.value = false }
}
async function remove(row: ImportScript) {
  try {
    await ElMessageBox.confirm(`确定删除格式化脚本“${row.name}”吗？`, '删除脚本', { type: 'warning' })
    await api.delete(`/import-scripts/${row.id}`)
    ElMessage.success('脚本已删除'); await load(); emit('changed')
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || '删除失败')
  }
}
watch(visible, (value) => { if (value) load() })
</script>

<template>
  <el-dialog v-model="visible" title="格式化脚本管理" width="min(1000px, 94vw)" append-to-body>
    <div class="script-toolbar">
      <p>脚本保存在服务端，在当前浏览器的隔离 Worker 中执行。</p>
      <el-button type="primary" @click="edit()">新增脚本</el-button>
    </div>
    <el-table v-loading="loading" :data="rows" max-height="520" empty-text="暂无格式化脚本">
      <el-table-column prop="name" label="名称" min-width="180"/>
      <el-table-column prop="description" label="说明" min-width="360" show-overflow-tooltip/>
      <el-table-column label="更新时间" width="180">
        <template #default="{ row }">{{ new Date(row.updated_at * 1000).toLocaleString() }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="edit(row)">编辑</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="editorVisible" :title="form.id ? '编辑格式化脚本' : '新增格式化脚本'"
      width="min(900px, 92vw)" append-to-body :close-on-click-modal="false">
      <el-form label-position="top">
        <el-form-item label="脚本名称"><el-input v-model="form.name" maxlength="100"/></el-form-item>
        <el-form-item label="用途说明"><el-input v-model="form.description" maxlength="500"/></el-form-item>
        <el-form-item label="JavaScript 代码">
          <el-input v-model="form.code" type="textarea" :rows="24" class="script-editor" spellcheck="false"/>
          <div class="form-tip">必须声明 async function format(raw, helpers) 并返回资源数组。可使用 fetch、Web Crypto 及 helpers.sha256、helpers.hmacSha256、helpers.base64Url、helpers.now。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="saving" @click="editorVisible=false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </el-dialog>
</template>
