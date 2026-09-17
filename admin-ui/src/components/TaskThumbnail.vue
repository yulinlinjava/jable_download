<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { Film } from '@element-plus/icons-vue'
import { api, type DownloadTask } from '../api'

const props = defineProps<{ task: DownloadTask }>()
const source = ref(props.task.cover_url || '')
const previewSource = ref(props.task.preview_url || '')
const failed = ref(false), previewFailed = ref(false), hovering = ref(false)
const video = ref<HTMLVideoElement>()
const objectUrls: string[] = []

onMounted(async () => {
  if (props.task.poster_file) {
    try {
      const response = await api.get(`/download/${props.task.task_id}/files/${props.task.poster_file}`, { responseType: 'blob' })
      const url = URL.createObjectURL(response.data); objectUrls.push(url); source.value = url
    } catch { /* keep the collected remote cover as fallback */ }
  }
  if (props.task.preview_file) {
    try {
      const response = await api.get(`/download/${props.task.task_id}/files/${props.task.preview_file}`, { responseType: 'blob' })
      const url = URL.createObjectURL(response.data); objectUrls.push(url); previewSource.value = url
      previewFailed.value = !response.data.type.startsWith('video/') && !/\.(mp4|webm)$/i.test(props.task.preview_file)
    } catch { /* keep the collected remote preview as fallback */ }
  }
})
async function enter() {
  hovering.value = true; await nextTick()
  if (!previewFailed.value) video.value?.play().catch(() => { previewFailed.value = true })
}
function leave() {
  hovering.value = false
  if (video.value) { video.value.pause(); video.value.currentTime = 0 }
}
onBeforeUnmount(() => objectUrls.forEach(URL.revokeObjectURL))
</script>

<template>
  <div class="download-thumb" @mouseenter="enter" @mouseleave="leave">
    <img v-if="source && !failed" class="thumb-poster" :src="source" alt="" @error="failed=true">
    <Film v-else class="thumb-placeholder" />
    <template v-if="hovering && previewSource">
      <video v-if="!previewFailed" ref="video" class="thumb-preview" :src="previewSource" muted loop playsinline preload="metadata" @error="previewFailed=true"/>
      <img v-else class="thumb-preview" :src="previewSource" alt=""/>
    </template>
    <span v-if="previewSource" class="preview-badge">预览</span>
  </div>
</template>
