<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, type FfmpegCompressionSettings, type FfmpegSettings, type PublicUrlSettings, type R2Settings, type SchedulerSettings } from '../api'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; updated: [] }>()
const visible = computed({ get: () => props.modelValue, set: value => emit('update:modelValue', value) })
const active = ref('access'), loading = ref(false), saving = ref(''), testing = ref(false)
const accessKey = ref('')
const publicUrl = reactive<PublicUrlSettings>({ url: '', source: 'default', environment_url: '', default_url: 'http://localhost:8000' })
const scheduler = reactive<SchedulerSettings>({ enabled: true, concurrency: 1, segment_concurrency: 3, running: 0, scheduled: 0, completed: 0, failed: 0, total: 0, download_speed: 0, upload_speed: 0, updated_at: 0 })
const ffmpeg = reactive<FfmpegSettings>({ concurrency: 1, maxConcurrency: 16, paused: false, running: 0, queued: 0, completed: 0, failed: 0, cancelled: 0 })
const compression = reactive<FfmpegCompressionSettings>({ enabled: true, minFileSizeMb: 500, minDurationMinutes: 30, maxVideoBitrateMbps: 5, maxWidth: 1920, maxHeight: 1080, crf: 25, preset: 'veryfast', audioBitrateKbps: 128, hlsSegmentSeconds: 3, presets: [] })
const r2 = reactive<R2Settings>({ enabled: false, endpointUrl: '', accessKeyId: '', hasSecretAccessKey: false, bucket: '', publicBaseUrl: '', pathPrefix: '', concurrency: 1, maxConcurrency: 16, fileConcurrency: 3, maxFileConcurrency: 16, autoUpload: false, autoCollection: false, cleanupEnabled: false, retentionDays: 7 })
const r2Secret = ref('')
const presetDescription = computed(() => ({
  ultrafast: '速度最快，文件通常较大', superfast: '极快，压缩率较低',
  veryfast: '速度与体积均衡，推荐', faster: '压缩率更好，速度稍慢',
  fast: '画质和体积更优，CPU耗时增加', medium: 'x264默认质量，处理较慢',
  slow: '压缩率高，处理很慢', slower: '压缩率更高，CPU耗时很长',
  veryslow: '压缩率最高，不建议普通服务器使用',
}[compression.preset] || ''))

async function load() {
  loading.value = true
  accessKey.value = localStorage.getItem('jable-api-key') || ''
  try {
    const [urlResult, schedulerResult, ffmpegResult, compressionResult, r2Result] = await Promise.all([
      api.get('/settings/public-url'), api.get('/settings/scheduler'), api.get('/ffmpeg/settings'),
      api.get('/ffmpeg/compression-settings'), api.get('/r2/settings'),
    ])
    Object.assign(publicUrl, urlResult.data.data)
    Object.assign(scheduler, schedulerResult.data.data)
    Object.assign(ffmpeg, ffmpegResult.data.data)
    Object.assign(compression, compressionResult.data.data)
    Object.assign(r2, r2Result.data.data)
    r2Secret.value = ''
  } catch (error: any) { ElMessage.error(error.message) }
  finally { loading.value = false }
}
watch(() => props.modelValue, value => { if (value) load() })

async function saveAccess() {
  const value = accessKey.value.trim()
  if (!value) return ElMessage.warning('请输入 X-API-Key')
  const previous = localStorage.getItem('jable-api-key') || ''
  saving.value = 'access'; localStorage.setItem('jable-api-key', value)
  try { await api.get('/auth/check'); ElMessage.success('API Key 已验证并保存'); emit('updated') }
  catch (error: any) { localStorage.setItem('jable-api-key', previous); ElMessage.error(error.message) }
  finally { saving.value = '' }
}
async function savePublicUrl() {
  saving.value = 'base'
  try { Object.assign(publicUrl, (await api.put('/settings/public-url', { url: publicUrl.url.trim() })).data.data); ElMessage.success('公开访问地址已保存'); emit('updated') }
  catch (error: any) { ElMessage.error(error.message) } finally { saving.value = '' }
}
async function saveScheduler() {
  saving.value = 'download'
  try { Object.assign(scheduler, (await api.put('/settings/scheduler', { enabled: scheduler.enabled, concurrency: scheduler.concurrency, segment_concurrency: scheduler.segment_concurrency })).data.data); ElMessage.success('下载调度配置已保存'); emit('updated') }
  catch (error: any) { ElMessage.error(error.message) } finally { saving.value = '' }
}
async function saveFfmpeg() {
  saving.value = 'ffmpeg'
  try {
    Object.assign(ffmpeg, (await api.put('/ffmpeg/settings', { concurrency: ffmpeg.concurrency })).data.data)
    Object.assign(compression, (await api.put('/ffmpeg/compression-settings', {
      enabled: compression.enabled, minFileSizeMb: compression.minFileSizeMb,
      minDurationMinutes: compression.minDurationMinutes, maxVideoBitrateMbps: compression.maxVideoBitrateMbps,
      maxWidth: compression.maxWidth, maxHeight: compression.maxHeight, crf: compression.crf,
      preset: compression.preset, audioBitrateKbps: compression.audioBitrateKbps,
      hlsSegmentSeconds: compression.hlsSegmentSeconds,
    })).data.data)
    ElMessage.success('FFmpeg 配置已保存'); emit('updated')
  } catch (error: any) { ElMessage.error(error.message) } finally { saving.value = '' }
}
const r2Payload = () => ({
  enabled: r2.enabled, endpointUrl: r2.endpointUrl, accessKeyId: r2.accessKeyId,
  secretAccessKey: r2Secret.value || null, bucket: r2.bucket, publicBaseUrl: r2.publicBaseUrl,
  pathPrefix: r2.pathPrefix, concurrency: r2.concurrency, fileConcurrency: r2.fileConcurrency,
  autoUpload: r2.autoUpload, autoCollection: r2.autoCollection,
  cleanupEnabled: r2.cleanupEnabled, retentionDays: r2.retentionDays,
})
async function testR2() {
  testing.value = true
  try { await api.post('/r2/settings/test', r2Payload()); ElMessage.success('R2 连接成功') }
  catch (error: any) { ElMessage.error(error.message) } finally { testing.value = false }
}
async function saveR2() {
  saving.value = 'r2'
  try { Object.assign(r2, (await api.put('/r2/settings', r2Payload())).data.data); r2Secret.value = ''; ElMessage.success('R2 配置已保存'); emit('updated') }
  catch (error: any) { ElMessage.error(error.message) } finally { saving.value = '' }
}
</script>

<template>
  <el-dialog v-model="visible" title="系统配置" width="min(920px, 94vw)" destroy-on-close :close-on-click-modal="false">
    <div v-loading="loading" class="system-settings">
      <el-tabs v-model="active" tab-position="left">
        <el-tab-pane label="访问鉴权" name="access">
          <el-form label-position="top"><el-form-item label="X-API-Key"><el-input v-model="accessKey" type="password" show-password autocomplete="current-password"/></el-form-item><el-button type="primary" :loading="saving==='access'" @click="saveAccess">验证并保存</el-button></el-form>
        </el-tab-pane>
        <el-tab-pane label="基础配置" name="base">
          <el-form label-position="top"><el-form-item label="公开访问地址"><el-input v-model="publicUrl.url" placeholder="http://localhost:8000"/><div class="form-tip">用于视频、封面、上传文件、导出数据和节点同步。</div></el-form-item><div class="setting-meta">环境变量：{{publicUrl.environment_url || '未设置'}} · 默认：{{publicUrl.default_url}}</div><el-button type="primary" :loading="saving==='base'" @click="savePublicUrl">保存基础配置</el-button></el-form>
        </el-tab-pane>
        <el-tab-pane label="下载调度" name="download">
          <el-form label-position="top"><el-form-item label="自动调度"><el-switch v-model="scheduler.enabled"/></el-form-item><el-form-item label="下载任务并发"><el-input-number v-model="scheduler.concurrency" :min="1" :max="10"/></el-form-item><el-form-item label="单任务 TS 分片并发"><el-input-number v-model="scheduler.segment_concurrency" :min="1" :max="16"/><div class="form-tip">默认3；总TS并发约为下载任务并发 × TS分片并发。</div></el-form-item><el-button type="primary" :loading="saving==='download'" @click="saveScheduler">保存下载配置</el-button></el-form>
        </el-tab-pane>
        <el-tab-pane label="FFmpeg" name="ffmpeg">
          <el-form label-position="top" class="settings-grid"><el-form-item label="任务并发数"><el-input-number v-model="ffmpeg.concurrency" :min="0" :max="ffmpeg.maxConcurrency"/><div class="form-tip">0 表示暂停领取新任务。</div></el-form-item><el-form-item label="启用自动压缩"><el-switch v-model="compression.enabled"/></el-form-item><el-form-item label="大文件判断（MB）"><el-input-number v-model="compression.minFileSizeMb" :min="1" :max="102400" :step="100"/></el-form-item><el-form-item label="长视频判断（分钟）"><el-input-number v-model="compression.minDurationMinutes" :min="1" :max="1440"/></el-form-item><el-form-item label="最大视频码率（Mbps）"><el-input-number v-model="compression.maxVideoBitrateMbps" :min="0.1" :max="100" :step="0.5"/></el-form-item><el-form-item label="最大分辨率"><div class="resolution-setting"><el-input-number v-model="compression.maxWidth" :min="320" :max="7680"/><span>×</span><el-input-number v-model="compression.maxHeight" :min="240" :max="4320"/></div></el-form-item><el-form-item label="CRF"><el-input-number v-model="compression.crf" :min="0" :max="51"/></el-form-item><el-form-item label="编码速度"><el-select v-model="compression.preset"><el-option v-for="item in compression.presets" :key="item" :label="item" :value="item"/></el-select><div class="form-tip">{{presetDescription}}</div></el-form-item><el-form-item label="AAC 音频码率（Kbps）"><el-input-number v-model="compression.audioBitrateKbps" :min="32" :max="512" :step="32"/></el-form-item><el-form-item label="HLS 分片时长（秒）"><el-input-number v-model="compression.hlsSegmentSeconds" :min="1" :max="30"/></el-form-item></el-form><el-button type="primary" :loading="saving==='ffmpeg'" @click="saveFfmpeg">保存 FFmpeg 配置</el-button>
        </el-tab-pane>
        <el-tab-pane label="Cloudflare R2" name="r2">
          <el-form label-position="top" class="settings-grid"><el-form-item label="启用 R2"><el-switch v-model="r2.enabled"/></el-form-item><el-form-item label="S3 Endpoint"><el-input v-model="r2.endpointUrl"/></el-form-item><el-form-item label="Access Key ID"><el-input v-model="r2.accessKeyId"/></el-form-item><el-form-item label="Secret Access Key"><el-input v-model="r2Secret" type="password" show-password :placeholder="r2.hasSecretAccessKey?'留空保持现有密钥':'请输入密钥'"/></el-form-item><el-form-item label="Bucket"><el-input v-model="r2.bucket"/></el-form-item><el-form-item label="R2 公开地址"><el-input v-model="r2.publicBaseUrl" placeholder="https://media.example.com"/></el-form-item><el-form-item label="对象路径前缀"><el-input v-model="r2.pathPrefix"/></el-form-item><el-form-item label="同步任务并发"><el-input-number v-model="r2.concurrency" :min="0" :max="r2.maxConcurrency"/></el-form-item><el-form-item label="单资源文件并发"><el-input-number v-model="r2.fileConcurrency" :min="1" :max="r2.maxFileConcurrency"/></el-form-item><el-form-item label="上传记录自动同步"><el-switch v-model="r2.autoUpload"/></el-form-item><el-form-item label="采集资源自动同步"><el-switch v-model="r2.autoCollection"/></el-form-item><el-form-item label="同步完成后自动删除本地文件"><el-switch v-model="r2.cleanupEnabled" :disabled="!r2.enabled"/><div class="form-tip">仅清理已确认同步完成的资源，数据库记录和 R2 文件会保留。</div></el-form-item><el-form-item label="本地文件保留天数"><el-input-number v-model="r2.retentionDays" :min="1" :max="3650" :disabled="!r2.enabled || !r2.cleanupEnabled"/><div class="form-tip">从 R2 实际同步完成时间开始计算。</div></el-form-item></el-form><div class="settings-actions"><el-button :loading="testing" @click="testR2">测试连接</el-button><el-button type="primary" :loading="saving==='r2'" @click="saveR2">保存 R2 配置</el-button></div>
        </el-tab-pane>
      </el-tabs>
    </div>
  </el-dialog>
</template>
