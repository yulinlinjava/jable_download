<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type ChunkCleanupSettings, type DatabaseSettings, type FfmpegCompressionSettings, type FfmpegSettings, type ImageCompressionSettings, type PublicUrlSettings, type R2Settings, type SchedulerSettings } from '../api'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; updated: [] }>()
const visible = computed({ get: () => props.modelValue, set: value => emit('update:modelValue', value) })
const active = ref('access'), loading = ref(false), saving = ref(''), testing = ref(false)
const accessKey = ref('')
const publicUrl = reactive<PublicUrlSettings>({ url: '', source: 'default', environment_url: '', default_url: 'http://localhost:8000' })
const database = reactive<DatabaseSettings>({ activePath: '', activeSize: 0, backupPath: '', backupExists: false, backupSize: 0, separateStorage: false })
const scheduler = reactive<SchedulerSettings>({ enabled: true, concurrency: 1, segment_concurrency: 3, running: 0, scheduled: 0, completed: 0, failed: 0, total: 0, download_speed: 0, upload_speed: 0, video_upload_speed: 0, other_upload_speed: 0, updated_at: 0 })
const ffmpeg = reactive<FfmpegSettings>({ concurrency: 1, maxConcurrency: 16, paused: false, running: 0, queued: 0, completed: 0, failed: 0, cancelled: 0 })
const compression = reactive<FfmpegCompressionSettings>({ enabled: true, compatibilityOnly: true, generateHoverPreview: true, generateVttPreview: true, minFileSizeMb: 500, minDurationMinutes: 30, maxVideoBitrateMbps: 5, maxWidth: 1920, maxHeight: 1920, crf: 25, preset: 'superfast', audioBitrateKbps: 128, hlsSegmentSeconds: 3, presets: [] })
const resolutionOptions = [
  { value: 360, label: '360P', size: '640×360', maxEdge: 640 },
  { value: 480, label: '480P', size: '854×480', maxEdge: 854 },
  { value: 720, label: '720P', size: '1280×720', maxEdge: 1280 },
  { value: 1080, label: '1080P', size: '1920×1080', maxEdge: 1920 },
  { value: 1440, label: '2K / 1440P', size: '2560×1440', maxEdge: 2560 },
  { value: 2160, label: '4K / 2160P', size: '3840×2160', maxEdge: 3840 },
]
const resolutionPreset = ref(1080)
const imageCompression = reactive<ImageCompressionSettings>({ enabled: true, minFileSizeMb: 5, quality: 82 })
const chunkCleanup = reactive<ChunkCleanupSettings>({ enabled: true, retentionDays: 7, incompleteCount: 0, incompleteBytes: 0, expiredCount: 0, lastCleanupAt: 0, lastDeleted: 0, lastFreedBytes: 0 })
const r2 = reactive<R2Settings>({ enabled: false, endpointUrl: '', accessKeyId: '', hasSecretAccessKey: false, bucket: '', publicBaseUrl: '', pathPrefix: '', videoConcurrency: 1, videoFileConcurrency: 3, otherConcurrency: 3, otherFileConcurrency: 3, maxConcurrency: 16, maxFileConcurrency: 16, autoUpload: false, autoCollection: false, cleanupEnabled: false, retentionDays: 7 })
const r2Secret = ref('')
let loadPromise: Promise<void> | undefined
const presetDescription = computed(() => ({
  ultrafast: '速度最快，文件通常较大', superfast: '极快，推荐用于降低 CPU 占用',
  veryfast: '速度与体积较均衡', faster: '压缩率更好，速度稍慢',
  fast: '画质和体积更优，CPU耗时增加', medium: 'x264默认质量，处理较慢',
  slow: '压缩率高，处理很慢', slower: '压缩率更高，CPU耗时很长',
  veryslow: '压缩率最高，不建议普通服务器使用',
}[compression.preset] || ''))
const resolutionDescription = computed(() => {
  const option = resolutionOptions.find(item => item.value === resolutionPreset.value)
  return option ? `横屏约 ${option.size}；竖屏自动交换宽高，始终保持原始比例。` : ''
})
function detectResolutionPreset(width: number, height: number) {
  const edge = Math.max(Number(width) || 0, Number(height) || 0)
  return (resolutionOptions.find(item => edge <= item.maxEdge)?.value
    || resolutionOptions[resolutionOptions.length - 1].value)
}
function applyResolutionPreset() {
  const option = resolutionOptions.find(item => item.value === resolutionPreset.value)
  if (!option) return
  compression.maxWidth = option.maxEdge
  compression.maxHeight = option.maxEdge
}
const formatBytes = (bytes: number) => {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']; let value = bytes; let index = 0
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1 }
  return `${value >= 10 ? value.toFixed(1) : value.toFixed(2)} ${units[index]}`
}
const formatTime = (value: number) => value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '尚未执行'

async function load() {
  if (loadPromise) return loadPromise
  const request = (async () => {
    loading.value = true
    accessKey.value = localStorage.getItem('jable-api-key') || ''
    try {
      const data = (await api.get('/settings/all')).data.data
      Object.assign(publicUrl, data.publicUrl)
      Object.assign(scheduler, data.scheduler)
      Object.assign(ffmpeg, data.ffmpeg)
      Object.assign(compression, data.compression)
      resolutionPreset.value = detectResolutionPreset(compression.maxWidth, compression.maxHeight)
      Object.assign(imageCompression, data.imageCompression)
      Object.assign(chunkCleanup, data.chunkCleanup)
      Object.assign(database, data.database)
      Object.assign(r2, data.r2)
      r2Secret.value = ''
    } catch (error: any) { ElMessage.error(error.message) }
    finally { loading.value = false }
  })()
  loadPromise = request
  try { await request }
  finally { if (loadPromise === request) loadPromise = undefined }
}
watch(() => props.modelValue, value => { if (value) void load() }, { immediate: true })

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
async function backupDatabase() {
  try {
    await ElMessageBox.confirm(
      `将当前数据库完整备份到 ${database.backupPath}，已有备份会被覆盖。`,
      '立即备份数据库',
      { type: 'warning', confirmButtonText: '开始备份', cancelButtonText: '取消' },
    )
  } catch { return }
  saving.value = 'database-backup'
  try {
    Object.assign(database, (await api.post('/database/backup')).data.data)
    ElMessage.success(`数据库已备份到 ${database.backupPath}`)
  } catch (error: any) { ElMessage.error(error.message) }
  finally { saving.value = '' }
}
async function saveScheduler() {
  saving.value = 'download'
  try { Object.assign(scheduler, (await api.put('/settings/scheduler', { enabled: scheduler.enabled, concurrency: scheduler.concurrency, segment_concurrency: scheduler.segment_concurrency })).data.data); ElMessage.success('下载调度配置已保存'); emit('updated') }
  catch (error: any) { ElMessage.error(error.message) } finally { saving.value = '' }
}
async function saveFfmpeg() {
  saving.value = 'ffmpeg'
  try {
    applyResolutionPreset()
    Object.assign(ffmpeg, (await api.put('/ffmpeg/settings', { concurrency: ffmpeg.concurrency })).data.data)
    Object.assign(compression, (await api.put('/ffmpeg/compression-settings', {
      enabled: compression.enabled, compatibilityOnly: compression.compatibilityOnly,
      generateHoverPreview: compression.generateHoverPreview,
      generateVttPreview: compression.generateVttPreview,
      minFileSizeMb: compression.minFileSizeMb,
      minDurationMinutes: compression.minDurationMinutes, maxVideoBitrateMbps: compression.maxVideoBitrateMbps,
      maxWidth: compression.maxWidth, maxHeight: compression.maxHeight, crf: compression.crf,
      preset: compression.preset, audioBitrateKbps: compression.audioBitrateKbps,
      hlsSegmentSeconds: compression.hlsSegmentSeconds,
    })).data.data)
    ElMessage.success('FFmpeg 配置已保存'); emit('updated')
  } catch (error: any) { ElMessage.error(error.message) } finally { saving.value = '' }
}
async function saveImageCompression() {
  saving.value = 'image'
  try {
    Object.assign(imageCompression, (await api.put('/image/compression-settings', {
      enabled: imageCompression.enabled,
      minFileSizeMb: imageCompression.minFileSizeMb,
      quality: imageCompression.quality,
    })).data.data)
    ElMessage.success('图片压缩配置已保存'); emit('updated')
  } catch (error: any) { ElMessage.error(error.message) } finally { saving.value = '' }
}
async function saveChunkCleanup() {
  saving.value = 'chunks'
  try {
    Object.assign(chunkCleanup, (await api.put('/upload/chunk-cleanup/settings', {
      enabled: chunkCleanup.enabled, retentionDays: chunkCleanup.retentionDays,
    })).data.data)
    ElMessage.success('未完成分片清理配置已保存')
  } catch (error: any) { ElMessage.error(error.message) } finally { saving.value = '' }
}
async function cleanExpiredChunks() {
  saving.value = 'chunks-run'
  try {
    const result = (await api.post('/upload/chunk-cleanup/run')).data.data
    Object.assign(chunkCleanup, (await api.get('/upload/chunk-cleanup/settings')).data.data)
    ElMessage.success(`已清理 ${result.deleted || 0} 个任务，释放 ${formatBytes(result.freedBytes || 0)}`)
  } catch (error: any) { ElMessage.error(error.message) } finally { saving.value = '' }
}
const r2Payload = () => ({
  enabled: r2.enabled, endpointUrl: r2.endpointUrl, accessKeyId: r2.accessKeyId,
  secretAccessKey: r2Secret.value || null, bucket: r2.bucket, publicBaseUrl: r2.publicBaseUrl,
  pathPrefix: r2.pathPrefix,
  videoConcurrency: r2.videoConcurrency, videoFileConcurrency: r2.videoFileConcurrency,
  otherConcurrency: r2.otherConcurrency, otherFileConcurrency: r2.otherFileConcurrency,
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
          <el-divider/>
          <div class="database-backup-panel">
            <div class="database-backup-info">
              <strong>数据库备份</strong>
              <div>运行数据库：{{database.activePath}}（{{formatBytes(database.activeSize)}}）</div>
              <div>备份文件：{{database.backupPath}}</div>
              <div>最近备份：{{database.backupExists ? `${formatTime(database.lastBackupAt || 0)} · ${formatBytes(database.backupSize)}` : '尚未备份'}}</div>
              <div class="form-tip">使用 SQLite 在线快照，备份时无需暂停下载；已有备份及配套密钥会被覆盖。</div>
            </div>
            <el-button type="primary" :loading="saving==='database-backup'" @click="backupDatabase">立即备份数据库</el-button>
          </div>
        </el-tab-pane>
        <el-tab-pane label="下载调度" name="download">
          <el-form label-position="top"><el-form-item label="自动调度"><el-switch v-model="scheduler.enabled"/></el-form-item><el-form-item label="下载任务并发"><el-input-number v-model="scheduler.concurrency" :min="1" :max="10"/></el-form-item><el-form-item label="单任务 TS 分片并发"><el-input-number v-model="scheduler.segment_concurrency" :min="1" :max="16"/><div class="form-tip">默认3；总TS并发约为下载任务并发 × TS分片并发。</div></el-form-item><el-button type="primary" :loading="saving==='download'" @click="saveScheduler">保存下载配置</el-button></el-form>
        </el-tab-pane>
        <el-tab-pane label="FFmpeg" name="ffmpeg">
          <el-form label-position="top" class="settings-grid">
            <el-form-item label="任务并发数"><el-input-number v-model="ffmpeg.concurrency" :min="0" :max="ffmpeg.maxConcurrency"/><div class="form-tip">默认 1；0 表示暂停领取新任务。</div></el-form-item>
            <el-form-item label="启用自动压缩"><el-switch v-model="compression.enabled"/></el-form-item>
            <el-form-item label="忽略大小与码率规则"><el-switch v-model="compression.compatibilityOnly" :disabled="!compression.enabled"/><div class="form-tip">开启后仅在编码不兼容或超过最大分辨率时转码。</div></el-form-item>
            <el-form-item label="生成列表悬浮预览"><el-switch v-model="compression.generateHoverPreview"/><div class="form-tip">关闭后不生成 preview_image.mp4。</div></el-form-item>
            <el-form-item label="生成播放器进度预览"><el-switch v-model="compression.generateVttPreview"/><div class="form-tip">关闭后不生成 preview.vtt 和预览雪碧图。</div></el-form-item>
            <el-form-item label="大文件判断（MB）"><el-input-number v-model="compression.minFileSizeMb" :min="1" :max="102400" :step="100" :disabled="compression.compatibilityOnly"/></el-form-item>
            <el-form-item label="长视频判断（分钟）"><el-input-number v-model="compression.minDurationMinutes" :min="1" :max="1440" :disabled="compression.compatibilityOnly"/></el-form-item>
            <el-form-item label="最大视频码率（Mbps）"><el-input-number v-model="compression.maxVideoBitrateMbps" :min="0.1" :max="100" :step="0.5" :disabled="compression.compatibilityOnly"/></el-form-item>
            <el-form-item label="输出清晰度"><el-select v-model="resolutionPreset" :disabled="!compression.enabled" @change="applyResolutionPreset"><el-option v-for="item in resolutionOptions" :key="item.value" :label="`${item.label}（${item.size}）`" :value="item.value"/></el-select><div class="form-tip">{{resolutionDescription}}</div></el-form-item>
            <el-form-item label="CRF"><el-input-number v-model="compression.crf" :min="0" :max="51"/></el-form-item>
            <el-form-item label="编码速度"><el-select v-model="compression.preset"><el-option v-for="item in compression.presets" :key="item" :label="item" :value="item"/></el-select><div class="form-tip">{{presetDescription}}</div></el-form-item>
            <el-form-item label="AAC 音频码率（Kbps）"><el-input-number v-model="compression.audioBitrateKbps" :min="32" :max="512" :step="32"/></el-form-item>
            <el-form-item label="HLS 分片时长（秒）"><el-input-number v-model="compression.hlsSegmentSeconds" :min="1" :max="30"/></el-form-item>
          </el-form><el-button type="primary" :loading="saving==='ffmpeg'" @click="saveFfmpeg">保存 FFmpeg 配置</el-button>
        </el-tab-pane>
        <el-tab-pane label="图片压缩" name="image">
          <el-form label-position="top" class="settings-grid"><el-form-item label="启用自动压缩"><el-switch v-model="imageCompression.enabled"/><div class="form-tip">仅处理保存配置后新上传的图片。</div></el-form-item><el-form-item label="压缩阈值（MB）"><el-input-number v-model="imageCompression.minFileSizeMb" :min="0.1" :max="102400" :step="1"/><div class="form-tip">图片大于该值时尝试压缩。</div></el-form-item><el-form-item label="JPEG / WebP 质量"><el-input-number v-model="imageCompression.quality" :min="1" :max="100"/><div class="form-tip">默认 82；PNG 始终使用无损压缩，GIF 动图保持原文件。</div></el-form-item></el-form><div class="form-tip image-compression-tip">压缩保持原格式、分辨率和宽高比。只有结果更小时才替换原图；失败时保留原图，上传仍然成功。</div><el-button type="primary" :loading="saving==='image'" @click="saveImageCompression">保存图片压缩配置</el-button>
        </el-tab-pane>
        <el-tab-pane label="上传分片" name="chunks">
          <el-form label-position="top" class="settings-grid"><el-form-item label="自动清理未完成分片"><el-switch v-model="chunkCleanup.enabled"/><div class="form-tip">只处理启用此功能后新上传或继续上传的任务，现有旧数据不会清理。</div></el-form-item><el-form-item label="无活动保留天数"><el-input-number v-model="chunkCleanup.retentionDays" :min="1" :max="3650"/><div class="form-tip">每收到一个新分片都会重新计算过期时间，默认 7 天。</div></el-form-item></el-form>
          <div class="setting-meta">受管理的未完成任务：{{chunkCleanup.incompleteCount}} · 占用：{{formatBytes(chunkCleanup.incompleteBytes)}} · 已过期：{{chunkCleanup.expiredCount}}</div>
          <div class="setting-meta">最近清理：{{formatTime(chunkCleanup.lastCleanupAt)}} · 删除 {{chunkCleanup.lastDeleted}} 个 · 释放 {{formatBytes(chunkCleanup.lastFreedBytes)}}</div>
          <div class="settings-actions"><el-button type="primary" :loading="saving==='chunks'" @click="saveChunkCleanup">保存分片清理配置</el-button><el-button :disabled="!chunkCleanup.enabled" :loading="saving==='chunks-run'" @click="cleanExpiredChunks">立即清理已过期分片</el-button></div>
        </el-tab-pane>
        <el-tab-pane label="Cloudflare R2" name="r2">
          <el-form label-position="top" class="settings-grid">
            <el-form-item label="启用 R2"><el-switch v-model="r2.enabled"/></el-form-item>
            <el-form-item label="S3 Endpoint"><el-input v-model="r2.endpointUrl"/></el-form-item>
            <el-form-item label="Access Key ID"><el-input v-model="r2.accessKeyId"/></el-form-item>
            <el-form-item label="Secret Access Key"><el-input v-model="r2Secret" type="password" show-password :placeholder="r2.hasSecretAccessKey?'留空保持现有密钥':'请输入密钥'"/></el-form-item>
            <el-form-item label="Bucket"><el-input v-model="r2.bucket"/></el-form-item>
            <el-form-item label="R2 公开地址"><el-input v-model="r2.publicBaseUrl" placeholder="https://media.example.com"/></el-form-item>
            <el-form-item label="对象路径前缀"><el-input v-model="r2.pathPrefix"/></el-form-item>
            <el-form-item label="视频同步任务并发"><el-input-number v-model="r2.videoConcurrency" :min="0" :max="r2.maxConcurrency"/><div class="form-tip">0 表示暂停视频组，不影响图片和其他文件。</div></el-form-item>
            <el-form-item label="单视频资源文件并发"><el-input-number v-model="r2.videoFileConcurrency" :min="1" :max="r2.maxFileConcurrency"/></el-form-item>
            <el-form-item label="其他同步任务并发"><el-input-number v-model="r2.otherConcurrency" :min="0" :max="r2.maxConcurrency"/><div class="form-tip">图片和其他文件使用独立队列；0 表示暂停该组。</div></el-form-item>
            <el-form-item label="单普通资源文件并发"><el-input-number v-model="r2.otherFileConcurrency" :min="1" :max="r2.maxFileConcurrency"/></el-form-item>
            <el-form-item label="上传记录自动同步"><el-switch v-model="r2.autoUpload"/></el-form-item>
            <el-form-item label="采集资源自动同步"><el-switch v-model="r2.autoCollection"/></el-form-item>
            <el-form-item label="同步完成后自动删除本地文件"><el-switch v-model="r2.cleanupEnabled" :disabled="!r2.enabled"/><div class="form-tip">仅在整个资源目录全部同步完成后清理本地文件，数据库记录和 R2 文件会保留。</div></el-form-item>
            <el-form-item label="本地文件保留天数"><el-input-number v-model="r2.retentionDays" :min="0" :max="3650" :disabled="!r2.enabled || !r2.cleanupEnabled"/><div class="form-tip">0 表示 R2 同步成功后立即删除；正数从实际同步完成时间开始计算。</div></el-form-item>
          </el-form><div class="settings-actions"><el-button :loading="testing" @click="testR2">测试连接</el-button><el-button type="primary" :loading="saving==='r2'" @click="saveR2">保存 R2 配置</el-button></div>
        </el-tab-pane>
      </el-tabs>
    </div>
  </el-dialog>
</template>

<style scoped>
.database-backup-panel { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.database-backup-info { min-width: 0; color: #697386; font-size: 13px; line-height: 1.8; word-break: break-all; }
.database-backup-info strong { display: block; margin-bottom: 4px; color: #303133; font-size: 14px; }
@media (max-width: 700px) { .database-backup-panel { flex-direction: column; } }
</style>
