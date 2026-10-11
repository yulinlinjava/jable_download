<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import Hls from 'hls.js'
import Plyr from 'plyr'
import { Download, Document } from '@element-plus/icons-vue'
import { uploadApi, type UploadResult } from '../api'

const props = defineProps<{ result: UploadResult }>()
const video = ref<HTMLVideoElement>()
const objectUrl = ref('')
const imagePath = ref(props.result.path)
const loading = ref(true)
let player: Plyr | undefined
let hls: Hls | undefined

async function loadBlob() {
  let response
  try { response = await uploadApi.get(props.result.path, { responseType: 'blob' }) }
  catch (error) {
    if (props.result.r2SyncStatus === 'synced' || props.result.r2Synced
        || !props.result.fallbackPath || props.result.fallbackPath === props.result.path) throw error
    response = await uploadApi.get(props.result.fallbackPath, { responseType: 'blob' })
  }
  objectUrl.value = URL.createObjectURL(response.data)
}

function loadVideoSource(source: string, fallback = false) {
  if (!video.value) return
  hls?.destroy()
  hls = undefined
  const fallbackSource = props.result.r2SyncStatus === 'synced' || props.result.r2Synced
    ? undefined : props.result.fallbackHlsPath
  if (Hls.isSupported()) {
    const key = localStorage.getItem('jable-api-key') || ''
    hls = new Hls({ xhrSetup: (xhr, url) => {
      if (key && new URL(url, window.location.origin).origin === window.location.origin) xhr.setRequestHeader('X-API-Key', key)
    } })
    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (data.fatal && !fallback && fallbackSource && fallbackSource !== source) loadVideoSource(fallbackSource, true)
    })
    hls.loadSource(source)
    hls.attachMedia(video.value)
  } else {
    const media = video.value
    if (!fallback && fallbackSource && fallbackSource !== source) {
      media.addEventListener('error', () => loadVideoSource(fallbackSource, true), { once: true })
    }
    media.src = source
  }
}

function imageError() {
  if (props.result.r2SyncStatus !== 'synced' && !props.result.r2Synced
      && props.result.fallbackPath && imagePath.value !== props.result.fallbackPath) {
    imagePath.value = props.result.fallbackPath
  }
}

async function downloadFile() {
  if (!objectUrl.value) await loadBlob()
  const link = document.createElement('a')
  link.href = objectUrl.value
  link.download = props.result.fileName
  link.click()
}

onMounted(async () => {
  try {
    if (props.result.previewType === 'video' && props.result.hlsPath) {
      await nextTick()
      player = new Plyr(video.value!, {
        controls: ['play-large', 'play', 'progress', 'current-time', 'duration', 'mute', 'volume', 'settings', 'pip', 'fullscreen'],
        poster: props.result.posterPath || props.result.coverPath,
        previewThumbnails: props.result.vttPath
          ? { enabled: true, src: props.result.vttPath }
          : undefined,
      })
      if (props.result.posterPath || props.result.coverPath) {
        const poster = props.result.posterPath || props.result.coverPath || ''
        video.value!.poster = poster
        const fallbackPoster = props.result.r2SyncStatus === 'synced' || props.result.r2Synced
          ? undefined : props.result.fallbackPosterPath || props.result.fallbackCoverPath
        if (fallbackPoster && fallbackPoster !== poster) {
          const probe = new Image()
          probe.onerror = () => {
            if (!video.value || !player) return
            video.value.poster = fallbackPoster
            player.poster = fallbackPoster
          }
          probe.src = poster
        }
      }
      loadVideoSource(props.result.hlsPath)
    } else if (['audio', 'pdf'].includes(props.result.previewType)) {
      await loadBlob()
    }
  } finally {
    loading.value = false
  }
})

onBeforeUnmount(() => {
  hls?.destroy()
  player?.destroy()
  if (objectUrl.value) URL.revokeObjectURL(objectUrl.value)
})
</script>

<template>
  <div class="upload-preview" v-loading="loading">
    <div v-if="result.previewType==='video' && result.hlsPath" class="player-wrap"><video ref="video" controls playsinline /></div>
    <img v-else-if="result.previewType==='image' && imagePath" :src="imagePath" :alt="result.fileName" @error="imageError" />
    <audio v-else-if="result.previewType==='audio' && objectUrl" :src="objectUrl" controls />
    <iframe v-else-if="result.previewType==='pdf' && objectUrl" :src="objectUrl" title="文件预览" />
    <el-empty v-else :image-size="72" description="该文件类型不支持在线预览"><template #image><el-icon :size="64"><Document/></el-icon></template></el-empty>
    <div v-if="result.warnings?.length" class="warning-text">{{result.warnings.join('；')}}</div>
    <div class="upload-result-bar"><div><strong>{{result.fileName}}</strong><span>{{((result.originalSize || result.size)/1024/1024).toFixed(2)}} MB<template v-if="result.durationText"> · {{result.durationText}}</template><template v-if="result.width && result.height"> · {{result.width}}×{{result.height}}</template><template v-if="result.videoCodec"> · {{result.videoCodec.toUpperCase()}}<template v-if="result.audioCodec">/{{result.audioCodec.toUpperCase()}}</template></template><span v-if="result.segmented"> · HLS {{result.segmentDuration}} 秒切片 · 已生成列表预览和进度预览 · 原视频已清理</span></span><span v-if="result.compressed">压缩后 {{((result.processedSize || 0)/1024/1024).toFixed(2)}} MB · 体积减少 {{result.compressionRatio || 0}}%</span><span v-else-if="result.processedSize">无需压缩 · <template v-if="result.previewType==='video'">HLS 分片</template><template v-else>最终大小</template> {{(result.processedSize/1024/1024).toFixed(2)}} MB</span></div><el-button v-if="!result.originalDeleted" :icon="Download" @click="downloadFile">下载文件</el-button></div>
  </div>
</template>
