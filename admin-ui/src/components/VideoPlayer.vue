<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import Hls from 'hls.js'
import Plyr from 'plyr'
import { api, type DownloadTask } from '../api'

const props = defineProps<{ task: DownloadTask }>()
const video = ref<HTMLVideoElement>()
let player: Plyr | undefined
let hls: Hls | undefined
const fallbackPoster = "data:image/svg+xml;charset=UTF-8," + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 900"><rect width="1600" height="900" fill="#111827"/><circle cx="800" cy="420" r="96" fill="#6366f1"/><path d="M775 365v110l90-55z" fill="white"/><text x="800" y="590" text-anchor="middle" fill="#a5b4fc" font-family="sans-serif" font-size="42">${props.task.title || props.task.video_id}</text></svg>`)
let posterUrl: string = props.task.cover_url || fallbackPoster
let localPosterUrl: string | undefined
const fileUrl = (name: string) => `/api/download/${props.task.task_id}/files/${name}`

onMounted(async () => {
  await nextTick()
  const key = localStorage.getItem('jable-api-key') || ''
  let mediaToken = ''
  if (key) {
    try { mediaToken = (await api.post(`/download/${props.task.task_id}/media-token`)).data.data.token }
    catch { /* HLS still uses the API key header and remains playable */ }
  }
  if (props.task.poster_file) {
    try {
      const response = await api.get(`/download/${props.task.task_id}/files/${props.task.poster_file}`, { responseType: 'blob' })
      localPosterUrl = URL.createObjectURL(response.data)
      posterUrl = localPosterUrl
    } catch { /* fall back to the collected cover or placeholder */ }
  }
  const options: Plyr.Options = {
    controls: ['play-large', 'play', 'progress', 'current-time', 'duration', 'mute', 'volume', 'settings', 'pip', 'fullscreen'],
    poster: posterUrl,
  }
  if (props.task.vtt_file) {
    options.previewThumbnails = { enabled: true, src: fileUrl(props.task.vtt_file) + (mediaToken ? `?token=${encodeURIComponent(mediaToken)}` : '') }
  }
  video.value!.poster = posterUrl
  player = new Plyr(video.value!, options)
  const stream = fileUrl('index.m3u8')
  if (Hls.isSupported()) {
    hls = new Hls({ xhrSetup: (xhr) => { if (key) xhr.setRequestHeader('X-API-Key', key) } })
    hls.loadSource(stream); hls.attachMedia(video.value!)
  } else if (video.value!.canPlayType('application/vnd.apple.mpegurl')) {
    video.value!.src = stream
  }
})
onBeforeUnmount(() => {
  hls?.destroy(); player?.destroy()
  if (localPosterUrl) URL.revokeObjectURL(localPosterUrl)
})
</script>

<template><div class="player-wrap"><video ref="video" controls playsinline /></div></template>
