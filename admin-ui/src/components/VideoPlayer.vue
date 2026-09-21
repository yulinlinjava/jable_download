<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import Hls from 'hls.js'
import Plyr from 'plyr'
import type { DownloadTask } from '../api'

const props = defineProps<{ task: DownloadTask }>()
const video = ref<HTMLVideoElement>()
let player: Plyr | undefined
let hls: Hls | undefined
const fallbackPoster = "data:image/svg+xml;charset=UTF-8," + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 900"><rect width="1600" height="900" fill="#111827"/><circle cx="800" cy="420" r="96" fill="#6366f1"/><path d="M775 365v110l90-55z" fill="white"/><text x="800" y="590" text-anchor="middle" fill="#a5b4fc" font-family="sans-serif" font-size="42">${props.task.title || props.task.video_id}</text></svg>`)
const mediaUrl = (value?: string) => value || ''

onMounted(async () => {
  await nextTick()
  const poster = mediaUrl(props.task.cover_url) || fallbackPoster
  const options: Plyr.Options = {
    controls: ['play-large', 'play', 'progress', 'current-time', 'duration', 'mute', 'volume', 'settings', 'pip', 'fullscreen'],
    poster,
  }
  const vtt = mediaUrl(props.task.vtt_url)
  if (vtt) options.previewThumbnails = { enabled: true, src: vtt }
  video.value!.poster = poster
  player = new Plyr(video.value!, options)
  const stream = mediaUrl(props.task.m3u8_url) || `/api/download/${props.task.task_id}/files/index.m3u8`
  if (Hls.isSupported()) {
    const key = localStorage.getItem('jable-api-key') || ''
    hls = new Hls({ xhrSetup: xhr => { xhr.setRequestHeader('X-API-Key', key) } })
    hls.loadSource(stream)
    hls.attachMedia(video.value!)
  } else if (video.value!.canPlayType('application/vnd.apple.mpegurl')) {
    video.value!.src = stream
  }
})
onBeforeUnmount(() => { hls?.destroy(); player?.destroy() })
</script>

<template><div class="player-wrap"><video ref="video" controls playsinline /></div></template>
