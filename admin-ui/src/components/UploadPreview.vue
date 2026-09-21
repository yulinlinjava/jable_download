<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import Hls from 'hls.js'
import Plyr from 'plyr'
import { Download, Document } from '@element-plus/icons-vue'
import { uploadApi, type UploadResult } from '../api'

const props = defineProps<{ result: UploadResult }>()
const video = ref<HTMLVideoElement>()
const objectUrl = ref('')
const loading = ref(true)
let player: Plyr | undefined
let hls: Hls | undefined

async function loadBlob() {
  const response = await uploadApi.get(props.result.path, { responseType: 'blob' })
  objectUrl.value = URL.createObjectURL(response.data)
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
      const key = localStorage.getItem('jable-api-key') || ''
      player = new Plyr(video.value!, {
        controls: ['play-large', 'play', 'progress', 'current-time', 'duration', 'mute', 'volume', 'settings', 'pip', 'fullscreen'],
        poster: props.result.posterPath || props.result.coverPath,
        previewThumbnails: props.result.vttPath
          ? { enabled: true, src: props.result.vttPath }
          : undefined,
      })
      if (props.result.posterPath || props.result.coverPath) {
        video.value!.poster = props.result.posterPath || props.result.coverPath || ''
      }
      if (Hls.isSupported()) {
        hls = new Hls({ xhrSetup: (xhr) => { if (key) xhr.setRequestHeader('X-API-Key', key) } })
        hls.loadSource(props.result.hlsPath)
        hls.attachMedia(video.value!)
      } else {
        video.value!.src = props.result.hlsPath
      }
    } else if (['image', 'audio', 'pdf'].includes(props.result.previewType)) {
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
    <img v-else-if="result.previewType==='image' && objectUrl" :src="objectUrl" :alt="result.fileName" />
    <audio v-else-if="result.previewType==='audio' && objectUrl" :src="objectUrl" controls />
    <iframe v-else-if="result.previewType==='pdf' && objectUrl" :src="objectUrl" title="文件预览" />
    <el-empty v-else :image-size="72" description="该文件类型不支持在线预览"><template #image><el-icon :size="64"><Document/></el-icon></template></el-empty>
    <div v-if="result.warnings?.length" class="warning-text">{{result.warnings.join('；')}}</div>
    <div class="upload-result-bar"><div><strong>{{result.fileName}}</strong><span>{{(result.size/1024/1024).toFixed(2)}} MB<span v-if="result.segmented"> · HLS {{result.segmentDuration}} 秒切片 · 已生成列表预览和进度预览 · 原视频已清理</span></span></div><el-button v-if="!result.originalDeleted" :icon="Download" @click="downloadFile">下载文件</el-button></div>
  </div>
</template>
