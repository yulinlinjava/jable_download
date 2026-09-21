<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CirclePlus } from '@element-plus/icons-vue'
import { api, type DownloadSigner } from '../api'

const rows = ref<DownloadSigner[]>([]), loading = ref(false), saving = ref(false), testing = ref(false)
const editorVisible = ref(false), configText = ref('{}'), testResult = ref('')
const form = reactive({ id: '', name: '', source: '', enabled: true, code: '',
  testUrl: 'https://video.example.com/path/index.m3u8', resourceType: 'playlist' })
const starter = `function sign(url, context, helpers, config) {
  const timestamp = helpers.now();
  const value = url + ':' + timestamp;
  const signature = helpers.hmacSha256(config.secret || '', value);

  return {
    url: helpers.withQuery(url, { timestamp, sign: signature }),
    headers: config.referer ? { Referer: config.referer } : {}
  };
}`

async function load() {
  loading.value = true
  try { rows.value = (await api.get('/download-signers')).data.data }
  catch (error: any) { ElMessage.error(error.message) }
  finally { loading.value = false }
}
function openEditor(row?: DownloadSigner) {
  Object.assign(form, row ? {
    id: row.id, name: row.name, source: row.source, enabled: row.enabled, code: row.code,
    testUrl: 'https://video.example.com/path/index.m3u8', resourceType: 'playlist',
  } : {
    id: '', name: '', source: '', enabled: true, code: starter,
    testUrl: 'https://video.example.com/path/index.m3u8', resourceType: 'playlist',
  })
  configText.value = JSON.stringify(row?.config || { secret: '', referer: '' }, null, 2)
  testResult.value = ''; editorVisible.value = true
}
function payload() {
  let config: unknown
  try { config = JSON.parse(configText.value || '{}') }
  catch (error: any) { throw new Error(`配置 JSON 格式错误：${error.message}`) }
  if (!config || Array.isArray(config) || typeof config !== 'object') throw new Error('签名配置必须是 JSON 对象')
  return { name: form.name.trim(), source: form.source.trim(), enabled: form.enabled,
    code: form.code, config }
}
async function save() {
  saving.value = true
  try {
    const body = payload()
    if (form.id) await api.put(`/download-signers/${form.id}`, body)
    else await api.post('/download-signers', body)
    ElMessage.success(form.id ? '签名脚本已保存' : '签名脚本已创建')
    editorVisible.value = false; await load()
  } catch (error: any) { ElMessage.error(error.message) }
  finally { saving.value = false }
}
async function testSigner() {
  testing.value = true; testResult.value = ''
  try {
    const body = { ...payload(), url: form.testUrl.trim(), resource_type: form.resourceType }
    const result = (await api.post('/download-signers/test', body)).data.data
    testResult.value = JSON.stringify(result, null, 2); ElMessage.success('签名脚本执行成功')
  } catch (error: any) { testResult.value = `执行失败：${error.message}`; ElMessage.error(error.message) }
  finally { testing.value = false }
}
async function toggle(row: DownloadSigner) {
  try {
    await api.put(`/download-signers/${row.id}`, {
      name: row.name, source: row.source, enabled: row.enabled, code: row.code, config: row.config,
    })
    ElMessage.success(row.enabled ? '签名脚本已启用' : '签名脚本已停用')
  } catch (error: any) { row.enabled = !row.enabled; ElMessage.error(error.message) }
}
async function remove(row: DownloadSigner) {
  try {
    await ElMessageBox.confirm(`删除 source=${row.source} 的签名脚本？后续下载将直接使用原始链接。`, '删除签名脚本', { type: 'warning' })
    await api.delete(`/download-signers/${row.id}`); ElMessage.success('签名脚本已删除'); await load()
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || '删除失败')
  }
}
onMounted(load)
</script>

<template>
  <el-card shadow="never" class="panel signer-panel">
    <template #header>
      <div class="panel-head">
        <div><h2>下载签名脚本</h2><p>按资源 source 匹配；数据库保存原始链接，脚本只在文件请求发出前执行。</p></div>
        <el-button type="primary" :icon="CirclePlus" @click="openEditor()">新增签名脚本</el-button>
      </div>
    </template>
    <el-alert title="每个 source 最多配置一个脚本。M3U8、TS、密钥、封面和 VTT 请求都会动态签名，签名 URL 不写入数据库。" type="info" :closable="false" show-icon/>
    <el-table v-loading="loading" :data="rows" empty-text="暂无签名脚本">
      <el-table-column prop="name" label="名称" min-width="200"/>
      <el-table-column label="匹配 source" min-width="180"><template #default="{ row }"><el-tag effect="plain">{{ row.source }}</el-tag></template></el-table-column>
      <el-table-column label="状态" width="130"><template #default="{ row }"><el-switch v-model="row.enabled" inline-prompt active-text="启用" inactive-text="停用" @change="toggle(row)"/></template></el-table-column>
      <el-table-column label="更新时间" width="190"><template #default="{ row }">{{ new Date(row.updated_at * 1000).toLocaleString() }}</template></el-table-column>
      <el-table-column label="操作" width="150" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="openEditor(row)">编辑</el-button><el-button link type="danger" @click="remove(row)">删除</el-button></template></el-table-column>
    </el-table>
  </el-card>

  <el-dialog v-model="editorVisible" :title="form.id ? '编辑签名脚本' : '新增签名脚本'"
    width="min(980px, 95vw)" :close-on-click-modal="false" destroy-on-close>
    <el-form label-position="top">
      <div class="signer-form-grid">
        <el-form-item label="名称"><el-input v-model="form.name" maxlength="100" placeholder="例如：合作方 A 签名"/></el-form-item>
        <el-form-item label="匹配 source"><el-input v-model="form.source" maxlength="100" placeholder="例如：partner_a"/></el-form-item>
      </div>
      <el-form-item label="启用"><el-switch v-model="form.enabled"/></el-form-item>
      <el-form-item label="签名配置 JSON">
        <el-input v-model="configText" type="textarea" :rows="5" class="script-editor" spellcheck="false"/>
        <div class="form-tip">配置在数据库中加密保存，通过 config 参数传给脚本。可以保存 secret、referer 等来源配置。</div>
      </el-form-item>
      <el-form-item label="JavaScript 签名函数">
        <el-input v-model="form.code" type="textarea" :rows="18" class="script-editor" spellcheck="false"/>
        <div class="form-tip">必须同步声明 function sign(url, context, helpers, config)。支持 md5、sha1、sha256、hmacSha1、hmacSha256、base64、base64Url、withQuery 和 now。</div>
      </el-form-item>
      <div class="signer-test-row">
        <el-input v-model="form.testUrl" placeholder="用于测试的原始资源 URL"/>
        <el-select v-model="form.resourceType" style="width:170px"><el-option v-for="kind in ['playlist','segment','key','cover','poster','preview_image','vtt','vtt_image']" :key="kind" :label="kind" :value="kind"/></el-select>
        <el-button :loading="testing" @click="testSigner">测试签名</el-button>
      </div>
      <el-input v-if="testResult" :model-value="testResult" type="textarea" :rows="5" readonly class="script-editor signer-test-result"/>
    </el-form>
    <template #footer><el-button :disabled="saving" @click="editorVisible=false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存</el-button></template>
  </el-dialog>
</template>
