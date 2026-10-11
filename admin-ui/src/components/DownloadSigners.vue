<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CirclePlus } from '@element-plus/icons-vue'
import { api, type DownloadSigner } from '../api'

const emit = defineEmits(['updated'])
const rows = ref<DownloadSigner[]>([]), loading = ref(false), saving = ref(false), testing = ref(false)
const editorVisible = ref(false), configText = ref('{}'), testResult = ref('')
const editorTab = ref('domains'), formatResult = ref(''), formatting = ref(false)
const rawInput = ref('{"items":[{"code":"demo-001","name":"示例资源","playUrl":"/video/demo/index.m3u8","coverUrl":"/images/demo.jpg"}]}')
const form = reactive({ id: '', name: '', source: '', enabled: true, code: '',
  image_base_url: '', video_base_url: '', format_code: '',
  testUrl: '/video/demo/index.m3u8', resourceType: 'playlist' })
const starter = `function sign(url, context, helpers, config) {
  // 不需要签名时直接返回；图片、视频可按 context.resource_group 分别处理。
  return { url, headers: config.referer ? { Referer: config.referer } : {} };
}`
const formatStarter = `function format(raw, context, config) {
  const items = Array.isArray(raw) ? raw : (raw.data?.list || raw.items || []);
  return items.map(item => ({
    source: context.source,
    id: String(item.id || item.code || ''),
    title: String(item.title || item.name || '').trim(),
    m3u8_url: item.m3u8_url || item.video_url || item.videoUrl || item.playUrl,
    cover: item.cover || item.coverUrl || null,
    poster: item.poster || item.posterUrl || null,
    preview_image: item.preview_image || item.previewUrl || null,
    vtt_url: item.vtt_url || item.vttUrl || null,
    url: item.url || item.detailUrl || null,
    duration: item.duration == null ? null : String(item.duration),
    published_at: item.published_at || item.publishedAt || null,
    actors: Array.isArray(item.actors) ? item.actors : [],
    tags: Array.isArray(item.tags) ? item.tags : [],
    chapters: Array.isArray(item.chapters) ? item.chapters : []
  }));
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
    image_base_url: row.image_base_url || '', video_base_url: row.video_base_url || '',
    format_code: row.format_code || '', testUrl: '/video/demo/index.m3u8', resourceType: 'playlist',
  } : {
    id: '', name: '', source: '', enabled: true, code: starter,
    image_base_url: '', video_base_url: '', format_code: '',
    testUrl: '/video/demo/index.m3u8', resourceType: 'playlist',
  })
  configText.value = JSON.stringify(row?.config || { secret: '', referer: '' }, null, 2)
  testResult.value = ''; formatResult.value = ''; editorTab.value = 'domains'; editorVisible.value = true
}
function payload() {
  let config: unknown
  try { config = JSON.parse(configText.value || '{}') }
  catch (error: any) { throw new Error(`配置 JSON 格式错误：${error.message}`) }
  if (!config || Array.isArray(config) || typeof config !== 'object') throw new Error('签名配置必须是 JSON 对象')
  return { name: form.name.trim(), source: form.source.trim(), enabled: form.enabled,
    code: form.code, config, image_base_url: form.image_base_url.trim(),
    video_base_url: form.video_base_url.trim(), format_code: form.format_code }
}
async function save() {
  saving.value = true
  try {
    const body = payload()
    if (form.id) await api.put(`/download-signers/${form.id}`, body)
    else await api.post('/download-signers', body)
    ElMessage.success(form.id ? '来源配置已保存' : '来源配置已创建')
    editorVisible.value = false; await load(); emit('updated')
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
      image_base_url: row.image_base_url, video_base_url: row.video_base_url, format_code: row.format_code,
    })
    ElMessage.success(row.enabled ? '来源配置已启用' : '来源配置已停用')
    emit('updated')
  } catch (error: any) { row.enabled = !row.enabled; ElMessage.error(error.message) }
}
async function testFormatter() {
  formatting.value = true
  try {
    const response = await api.post('/download-signers/format-test', { ...payload(), raw: JSON.parse(rawInput.value) })
    formatResult.value = JSON.stringify(response.data.data.items, null, 2)
    ElMessage.success('格式化预览成功，未导入资源')
  } catch (error: any) { ElMessage.error(error.message) }
  finally { formatting.value = false }
}
async function remove(row: DownloadSigner) {
  try {
    await ElMessageBox.confirm(`删除 source=${row.source} 的配置？将停止使用该来源的域名、格式化和签名规则。`, '删除来源配置', { type: 'warning' })
    await api.delete(`/download-signers/${row.id}`); ElMessage.success('来源配置已删除'); await load(); emit('updated')
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
        <div><h2>来源管理</h2><p>按 source 统一配置资源域名、数据格式化和动态签名。</p></div>
        <el-button type="primary" :icon="CirclePlus" @click="openEditor()">新增来源</el-button>
      </div>
    </template>
    <el-alert title="导入时格式化并保留原始路径；远程预览和下载时补域名、动态签名。下载完成后使用本地资源。" type="info" :closable="false" show-icon/>
    <el-table v-loading="loading" :data="rows" empty-text="暂无来源配置">
      <el-table-column prop="name" label="名称" min-width="200"/>
      <el-table-column label="匹配 source" min-width="180"><template #default="{ row }"><el-tag effect="plain">{{ row.source }}</el-tag></template></el-table-column>
      <el-table-column prop="image_base_url" label="图片域名" min-width="210" show-overflow-tooltip/>
      <el-table-column prop="video_base_url" label="视频域名" min-width="210" show-overflow-tooltip/>
      <el-table-column label="脚本" width="150"><template #default="{row}"><span>{{row.format_code ? '格式化' : '直接导入'}} · {{row.code ? '签名' : '无需签名'}}</span></template></el-table-column>
      <el-table-column label="状态" width="130"><template #default="{ row }"><el-switch v-model="row.enabled" inline-prompt active-text="启用" inactive-text="停用" @change="toggle(row)"/></template></el-table-column>
      <el-table-column label="更新时间" width="190"><template #default="{ row }">{{ new Date(row.updated_at * 1000).toLocaleString() }}</template></el-table-column>
      <el-table-column label="操作" width="150" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="openEditor(row)">编辑</el-button><el-button link type="danger" @click="remove(row)">删除</el-button></template></el-table-column>
    </el-table>
  </el-card>

  <el-dialog v-model="editorVisible" :title="form.id ? '编辑来源' : '新增来源'"
    width="min(980px, 95vw)" :close-on-click-modal="false" destroy-on-close>
    <el-form label-position="top">
      <div class="signer-form-grid">
        <el-form-item label="名称"><el-input v-model="form.name" maxlength="100" placeholder="例如：合作方 A 签名"/></el-form-item>
        <el-form-item label="匹配 source"><el-input v-model="form.source" maxlength="100" placeholder="例如：partner_a"/></el-form-item>
      </div>
      <el-form-item label="启用"><el-switch v-model="form.enabled"/></el-form-item>
      <el-tabs v-model="editorTab">
      <el-tab-pane label="域名与参数" name="domains">
      <el-form-item label="图片域名"><el-input v-model="form.image_base_url" placeholder="https://img.example.com（封面、图片预览、VTT雪碧图）"/></el-form-item>
      <el-form-item label="视频域名"><el-input v-model="form.video_base_url" placeholder="https://video.example.com（M3U8、TS、预览视频、VTT和密钥）"/></el-form-item>
      <p class="form-tip">相对路径访问时补上对应域名。完整 HTTP/HTTPS 地址默认保留原域名，可由签名脚本改写。</p>
      <el-form-item label="签名配置 JSON">
        <el-input v-model="configText" type="textarea" :rows="5" class="script-editor" spellcheck="false"/>
        <div class="form-tip">配置在数据库中加密保存，通过 config 参数传给脚本。可以保存 secret、referer 等来源配置。</div>
      </el-form-item>
      </el-tab-pane>
      <el-tab-pane label="数据格式化" name="formatter">
        <el-form-item label="JavaScript 格式化函数（可选）"><el-input v-model="form.format_code" type="textarea" :rows="16" class="script-editor" spellcheck="false"/><div class="form-tip">同步声明 function format(raw, context, config)，返回标准资源数组。context.source 是当前来源；留空直接校验标准数据，执行最长3秒。</div></el-form-item>
        <el-button @click="form.format_code=formatStarter">填入字段映射示例</el-button>
        <el-form-item label="第三方原始 JSON"><el-input v-model="rawInput" type="textarea" :rows="6" spellcheck="false"/></el-form-item>
        <el-button :loading="formatting" @click="testFormatter">格式化并预览</el-button>
        <el-input v-if="formatResult" :model-value="formatResult" type="textarea" :rows="10" readonly class="script-editor signer-test-result"/>
      </el-tab-pane>
      <el-tab-pane label="链接签名" name="signer">
      <el-form-item label="JavaScript 签名函数">
        <el-input v-model="form.code" type="textarea" :rows="18" class="script-editor" spellcheck="false"/>
        <div class="form-tip">同步声明 function sign(url, context, helpers, config)。url 已补域名，context.original_url 保留原路径，context.resource_group 为 image 或 video。留空不签名。支持 md5、sha1、sha256、hmacSha1、hmacSha256、base64、base64Url、withQuery 和 now。</div>
      </el-form-item>
      <div class="signer-test-row">
        <el-input v-model="form.testUrl" placeholder="用于测试的原始资源 URL 或相对路径"/>
        <el-select v-model="form.resourceType" style="width:170px"><el-option v-for="kind in ['playlist','segment','key','map','cover','poster','preview_image','vtt','vtt_image']" :key="kind" :label="kind" :value="kind"/></el-select>
        <el-button :loading="testing" @click="testSigner">测试签名</el-button>
      </div>
      <el-input v-if="testResult" :model-value="testResult" type="textarea" :rows="5" readonly class="script-editor signer-test-result"/>
      </el-tab-pane>
      </el-tabs>
    </el-form>
    <template #footer><el-button :disabled="saving" @click="editorVisible=false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存</el-button></template>
  </el-dialog>
</template>
