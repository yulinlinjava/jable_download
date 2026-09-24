<script setup>
import { computed, ref } from 'vue'

// Vue 直连上传服务：替换为实际地址和 JABLE_API_KEY。
const API_BASE = 'http://localhost:8000'
const API_KEY = '请替换为服务端配置的 JABLE_API_KEY'
const CHUNK_SIZE = 10 * 1024 * 1024

const selectedFile = ref(null)
const uploading = ref(false)
const percent = ref(0)
const message = ref('请选择文件')
const result = ref(null)
const error = ref('')
const displaySize = computed(() => selectedFile.value
  ? `${(selectedFile.value.size / 1024 / 1024).toFixed(2)} MiB`
  : '')

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {})
  headers.set('X-API-Key', API_KEY)
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers })
  const payload = await response.json().catch(() => null)
  if (!response.ok || !payload || payload.code !== 0) {
    throw new Error(payload?.message || `请求失败：HTTP ${response.status}`)
  }
  return payload.data
}

// 普通 HTTP 无 crypto.subtle 时，仅用文件元信息生成稳定的 FNV-1a 64 位 ID。
function createMetadataIdentifier(file) {
  const bytes = new TextEncoder().encode(`${file.name}\n${file.size}\n${file.lastModified}`)
  let hash = 0xcbf29ce484222325n
  for (const byte of bytes) {
    hash ^= BigInt(byte)
    hash = BigInt.asUintN(64, hash * 0x100000001b3n)
  }
  return hash.toString(16).padStart(16, '0')
}

// HTTPS/localhost：文件大小 + 首尾各 16 KiB，SHA-256 后截取前 8 字节。
async function createIdentifier(file) {
  if (!globalThis.crypto?.subtle) return createMetadataIdentifier(file)
  const sampleSize = 16 * 1024
  const sizeBytes = new TextEncoder().encode(`${file.size}\n`)
  const samples = file.size <= sampleSize * 2
    ? [await file.arrayBuffer()]
    : await Promise.all([
        file.slice(0, sampleSize).arrayBuffer(),
        file.slice(file.size - sampleSize).arrayBuffer(),
      ])
  const length = sizeBytes.byteLength
    + samples.reduce((total, sample) => total + sample.byteLength, 0)
  const bytes = new Uint8Array(length)
  bytes.set(sizeBytes)
  let offset = sizeBytes.byteLength
  for (const sample of samples) {
    bytes.set(new Uint8Array(sample), offset)
    offset += sample.byteLength
  }
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest).subarray(0, 8))
    .map(value => value.toString(16).padStart(2, '0')).join('')
}

const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))

async function retry(action, attempts = 3) {
  let lastError
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await action()
    } catch (current) {
      lastError = current
      if (attempt < attempts) await sleep(attempt * 800)
    }
  }
  throw lastError
}

async function query(identifier) {
  return request(`/upload/chunk?identifier=${encodeURIComponent(identifier)}`)
}

async function uploadChunk(file, identifier, number, total) {
  const start = (number - 1) * CHUNK_SIZE
  const body = new FormData()
  body.append('identifier', identifier)
  body.append('chunkNumber', String(number))
  body.append('totalChunks', String(total))
  body.append('file', file.slice(start, Math.min(file.size, start + CHUNK_SIZE)))
  return retry(() => request('/upload/chunk', { method: 'POST', body }))
}

async function merge(file, identifier) {
  return request('/upload/merge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ identifier, fileName: file.name }),
  })
}

async function waitForCompletion(identifier) {
  while (true) {
    await sleep(1500)
    const status = await query(identifier)
    message.value = status.message || status.processingStage || status.status
    if (['failed', 'cancelled'].includes(status.status)) {
      throw new Error(status.error || '后台处理失败')
    }
    if (status.completed && status.status === 'completed' && status.path) return status
  }
}

function chooseFile(event) {
  selectedFile.value = event.target.files?.[0] || null
  result.value = null
  error.value = ''
  percent.value = 0
  message.value = selectedFile.value ? '等待上传' : '请选择文件'
}

async function startUpload() {
  const file = selectedFile.value
  if (!file || uploading.value) return
  uploading.value = true
  error.value = ''
  result.value = null
  try {
    message.value = '正在计算文件标识'
    const identifier = await createIdentifier(file)
    const totalChunks = Math.max(1, Math.ceil(file.size / CHUNK_SIZE))
    const current = await query(identifier)
    if (current.completed && current.status === 'completed' && current.path) {
      percent.value = 100
      message.value = '文件已经上传完成'
      result.value = current
      return
    }
    const uploaded = new Set(current.uploadedChunks || [])
    for (let number = 1; number <= totalChunks; number += 1) {
      if (!uploaded.has(number)) await uploadChunk(file, identifier, number, totalChunks)
      percent.value = Math.round(number / totalChunks * 90)
      message.value = `已上传分片 ${number} / ${totalChunks}`
    }
    message.value = '正在提交合并任务'
    await merge(file, identifier)
    percent.value = 95
    result.value = await waitForCompletion(identifier)
    percent.value = 100
    message.value = '上传及后台处理完成'
  } catch (current) {
    error.value = current instanceof Error ? current.message : String(current)
    message.value = '上传失败，可以再次点击继续上传'
  } finally {
    uploading.value = false
  }
}
</script>

<template>
  <section class="uploader">
    <input type="file" @change="chooseFile">
    <span v-if="selectedFile">{{ selectedFile.name }} · {{ displaySize }}</span>
    <button :disabled="!selectedFile || uploading" @click="startUpload">
      {{ uploading ? '处理中…' : '开始上传' }}
    </button>
    <progress :value="percent" max="100" />
    <div>{{ percent }}% · {{ message }}</div>
    <div v-if="error" class="error">{{ error }}</div>
    <a v-if="result?.path" :href="result.path" target="_blank" rel="noopener">
      打开上传结果
    </a>
  </section>
</template>

<style scoped>
.uploader { display: grid; gap: 12px; max-width: 560px; }
progress { width: 100%; }
.error { color: #dc2626; }
</style>
