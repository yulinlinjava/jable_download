<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import Hls from 'hls.js'
import Plyr from 'plyr'
import type { DownloadTask } from '../api'

const props = defineProps<{ task: DownloadTask }>()
const video = ref<HTMLVideoElement>()
const selectedChapter = ref(0)
const chapterLoading = ref(false)
const chapterError = ref('')
let player: Plyr | undefined
let hls: Hls | undefined
let loadSequence = 0
const fallbackPoster = "data:image/svg+xml;charset=UTF-8," + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 900"><rect width="1600" height="900" fill="#111827"/><circle cx="800" cy="420" r="96" fill="#6366f1"/><path d="M775 365v110l90-55z" fill="white"/><text x="800" y="590" text-anchor="middle" fill="#a5b4fc" font-family="sans-serif" font-size="42">${props.task.title || props.task.video_id}</text></svg>`)
const mediaUrl = (value?: string) => value || ''
const chapters = computed(() => props.task.chapters?.length ? props.task.chapters : [{
  index: 1, title: '第 1 章', status: props.task.status,
  downloaded_segments: props.task.downloaded_segments,
  total_segments: props.task.total_segments, progress: props.task.progress,
  m3u8_url: props.task.m3u8_url, vtt_url: props.task.vtt_url,
}])

function clearMedia() {
  hls?.destroy()
  hls = undefined
  const media = video.value
  if (!media) return
  media.pause()
  media.removeAttribute('src')
  media.load()
}

async function loadChapter(index: number, autoplay = true, fallback = false) {
  const chapter = chapters.value[index]
  const media = video.value
  if (!chapter || !media || !player) return
  const sequence = ++loadSequence
  selectedChapter.value = index
  chapterLoading.value = true
  chapterError.value = ''
  clearMedia()
  const vtt = mediaUrl(fallback ? chapter.fallback_vtt_url : chapter.vtt_url)
  player.setPreviewThumbnails({ enabled: Boolean(vtt), src: vtt })
  const stream = fallback
    ? (mediaUrl(chapter.fallback_m3u8_url) || mediaUrl(props.task.fallback_m3u8_url))
    : (mediaUrl(chapter.m3u8_url) || mediaUrl(props.task.m3u8_url) || `/api/download/${props.task.task_id}/files/index.m3u8`)
  if (!stream) {
    chapterLoading.value = false
    chapterError.value = `第 ${index + 1} 章缺少播放地址`
    return
  }
  if (Hls.isSupported()) {
    const key = localStorage.getItem('jable-api-key') || ''
    hls = new Hls({ xhrSetup: (xhr, url) => {
      if (key && new URL(url, window.location.origin).origin === window.location.origin) xhr.setRequestHeader('X-API-Key', key)
    } })
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      if (sequence !== loadSequence) return
      chapterLoading.value = false
      if (autoplay) Promise.resolve(player?.play()).catch(() => undefined)
    })
    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (sequence !== loadSequence || !data.fatal) return
      if (!fallback && (chapter.fallback_m3u8_url || props.task.fallback_m3u8_url)) {
        loadChapter(index, autoplay, true)
        return
      }
      chapterLoading.value = false
      chapterError.value = `第 ${index + 1} 章加载失败：${data.details || data.type}`
    })
    hls.loadSource(stream)
    hls.attachMedia(media)
  } else if (media.canPlayType('application/vnd.apple.mpegurl')) {
    const loaded = () => {
      if (sequence !== loadSequence) return
      chapterLoading.value = false
      if (autoplay) Promise.resolve(player?.play()).catch(() => undefined)
    }
    const failed = () => {
      if (sequence !== loadSequence) return
      if (!fallback && (chapter.fallback_m3u8_url || props.task.fallback_m3u8_url)) {
        loadChapter(index, autoplay, true)
        return
      }
      chapterLoading.value = false
      chapterError.value = `第 ${index + 1} 章加载失败，请检查播放地址`
    }
    media.addEventListener('loadedmetadata', loaded, { once: true })
    media.addEventListener('error', failed, { once: true })
    media.src = stream
    media.load()
  } else {
    chapterLoading.value = false
    chapterError.value = '当前浏览器不支持 HLS 视频播放'
  }
}
onMounted(async () => {
  await nextTick()
  const poster = mediaUrl(props.task.cover_url) || fallbackPoster
  video.value!.poster = poster
  player = new Plyr(video.value!, {
    controls: ['play-large', 'play', 'progress', 'current-time', 'duration', 'mute', 'volume', 'settings', 'pip', 'fullscreen'],
    poster,
    previewThumbnails: { enabled: false, src: '' },
  })
  if (props.task.fallback_cover_url && poster !== props.task.fallback_cover_url) {
    const probe = new Image()
    probe.onerror = () => {
      if (!video.value || !player) return
      video.value.poster = props.task.fallback_cover_url || fallbackPoster
      player.poster = props.task.fallback_cover_url || fallbackPoster
    }
    probe.src = poster
  }
  await loadChapter(0, false)
})
onBeforeUnmount(() => { loadSequence += 1; clearMedia(); player?.destroy() })
</script>

<template>
  <div class="player-wrap">
    <video ref="video" controls playsinline />
    <div v-if="chapters.length > 1" class="chapter-panel">
      <div class="chapter-heading"><strong>章节</strong><span v-if="chapterLoading">正在加载第 {{selectedChapter + 1}} 章…</span><span v-else>正在播放：{{chapters[selectedChapter]?.title}}</span></div>
      <div class="chapter-list">
        <button v-for="(chapter, index) in chapters" :key="chapter.index"
                type="button" :class="{active:selectedChapter===index}" :disabled="chapterLoading && selectedChapter===index"
                @click="loadChapter(index)">
          {{chapter.title || `第 ${index + 1} 章`}}
        </button>
      </div>
      <div v-if="chapterError" class="chapter-error">{{chapterError}}</div>
    </div>
  </div>
</template>

<style scoped>
.chapter-panel{padding:14px 16px 16px;background:#fff;color:#374151}
.chapter-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:10px;font-size:13px}.chapter-heading span{color:#8b93a4;font-size:12px}
.chapter-list{display:flex;gap:8px;overflow-x:auto;padding:1px 0 4px}
.chapter-list button{flex:0 0 auto;border:1px solid #d1d5db;border-radius:7px;background:#fff;color:#374151;padding:7px 14px;cursor:pointer}
.chapter-list button.active{border-color:#6366f1;background:#6366f1;color:#fff}
.chapter-list button:disabled{cursor:wait;opacity:.72}
.chapter-error{margin-top:10px;color:#ef5350;font-size:12px}
</style>
