<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Film } from '@element-plus/icons-vue'
import Hls from 'hls.js'

const props = defineProps<{ cover?: string; preview?: string; previewHls?: string }>()
const failed = ref(false), previewFailed = ref(false), hlsFailed = ref(false), hovering = ref(false)
const video = ref<HTMLVideoElement>()
let hls: Hls | undefined
const source = computed(() => props.cover || '')
const previewSource = computed(() => props.preview || '')
const hlsSource = computed(() => props.previewHls || '')
const useHls = computed(() => Boolean(hlsSource.value && !hlsFailed.value))
const previewIsVideo = computed(() => useHls.value || /\.(mp4|webm)(?:\?|$)/i.test(previewSource.value))
const hasPreview = computed(() => Boolean(hlsSource.value || previewSource.value))
watch(source, () => { failed.value = false })
watch([previewSource, hlsSource], () => {
  stopVideo()
  previewFailed.value = false
  hlsFailed.value = false
})

function stopVideo() {
  hls?.destroy()
  hls = undefined
  if (video.value) {
    video.value.pause()
    video.value.removeAttribute('src')
    video.value.load()
  }
}

async function playFallback() {
  await nextTick()
  if (previewIsVideo.value && !previewFailed.value) {
    video.value?.play().catch(() => { previewFailed.value = true })
  }
}

async function enter() {
  hovering.value = true
  await nextTick()
  if (!video.value || !previewIsVideo.value || previewFailed.value) return
  if (useHls.value) {
    if (Hls.isSupported()) {
      hls = new Hls()
      hls.on(Hls.Events.MANIFEST_PARSED, () => video.value?.play().catch(() => {}))
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal) return
        hls?.destroy(); hls = undefined; hlsFailed.value = true
        if (previewSource.value) playFallback()
        else previewFailed.value = true
      })
      hls.loadSource(hlsSource.value)
      hls.attachMedia(video.value)
      return
    }
    if (video.value.canPlayType('application/vnd.apple.mpegurl')) {
      video.value.src = hlsSource.value
      video.value.play().catch(() => { hlsFailed.value = true; playFallback() })
      return
    }
    hlsFailed.value = true
  }
  await playFallback()
}
function leave() {
  hovering.value = false
  stopVideo()
}
onBeforeUnmount(stopVideo)
</script>

<template>
  <div class="download-thumb" @mouseenter="enter" @mouseleave="leave">
    <img v-if="source && !failed" class="thumb-poster" :src="source" alt="" @error="failed=true">
    <Film v-else class="thumb-placeholder" />
    <template v-if="hovering && hasPreview && !previewFailed">
      <video v-if="previewIsVideo" ref="video" class="thumb-preview" :src="useHls ? undefined : previewSource" muted loop playsinline preload="metadata" @error="previewFailed=true"/>
      <img v-else class="thumb-preview" :src="previewSource" alt="" @error="previewFailed=true"/>
    </template>
    <span v-if="hasPreview" class="preview-badge">预览</span>
  </div>
</template>
