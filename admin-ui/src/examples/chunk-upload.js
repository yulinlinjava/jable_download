// 浏览器原生 JavaScript 分片上传示例
const API_BASE = 'http://localhost:8000'
const API_KEY = '请替换为服务端配置的 JABLE_API_KEY'
const CHUNK_SIZE = 10 * 1024 * 1024

async function apiRequest(path, options = {}) {
  const headers = new Headers(options.headers || {})
  headers.set('X-API-Key', API_KEY)
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers })
  const payload = await response.json()
  if (!response.ok || payload.code !== 0) {
    throw new Error(payload.message || `请求失败：HTTP ${response.status}`)
  }
  return payload.data
}

// 只对文件元信息计算哈希，不读取整个大文件。
// 同一文件再次选择时会得到相同 ID，从而查询并跳过已上传分片。
async function createIdentifier(file) {
  const text = `${file.name}\n${file.size}\n${file.lastModified}`
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return Array.from(new Uint8Array(digest).slice(0, 8))
    .map(value => value.toString(16).padStart(2, '0'))
    .join('')
}

async function queryUpload(identifier) {
  return apiRequest(`/upload/chunk?identifier=${encodeURIComponent(identifier)}`)
}

async function sendChunk(file, identifier, chunkNumber, totalChunks) {
  const start = (chunkNumber - 1) * CHUNK_SIZE
  const body = new FormData()
  body.append('identifier', identifier)
  body.append('chunkNumber', String(chunkNumber))
  body.append('totalChunks', String(totalChunks))
  body.append('file', file.slice(start, Math.min(file.size, start + CHUNK_SIZE)), file.name)
  return apiRequest('/upload/chunk', { method: 'POST', body })
}

async function submitMerge(file, identifier, totalChunks) {
  return apiRequest('/upload/merge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      identifier,
      fileName: file.name,
      totalChunks,
    }),
  })
}

const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))

async function waitUntilCompleted(identifier, onProgress) {
  while (true) {
    await wait(1000)
    const status = await queryUpload(identifier)
    onProgress({
      stage: status.processingStage || status.status,
      percent: status.completed ? 100 : 95,
      message: status.message,
    })
    if (status.status === 'failed') {
      throw new Error(status.error || '后台合并或视频处理失败')
    }
    if (status.completed && status.status === 'completed' && status.path) {
      return status
    }
  }
}

// 返回最终文件信息。视频包含 hlsPath、coverPath、previewPath 和 vttPath。
export async function uploadInChunks(file, onProgress = console.log) {
  if (!(file instanceof File)) throw new TypeError('file 必须是浏览器 File 对象')
  const identifier = await createIdentifier(file)
  const totalChunks = Math.max(1, Math.ceil(file.size / CHUNK_SIZE))
  const current = await queryUpload(identifier)

  if (current.completed && current.status === 'completed' && current.path) {
    onProgress({ stage: 'completed', percent: 100, message: '文件已经上传完成' })
    return current
  }

  const uploaded = new Set(current.uploadedChunks || [])
  for (let chunkNumber = 1; chunkNumber <= totalChunks; chunkNumber += 1) {
    if (!uploaded.has(chunkNumber)) {
      await sendChunk(file, identifier, chunkNumber, totalChunks)
    }
    onProgress({
      stage: 'uploading',
      percent: Math.round(chunkNumber / totalChunks * 90),
      message: `已上传分片 ${chunkNumber} / ${totalChunks}`,
    })
  }

  const merge = await submitMerge(file, identifier, totalChunks)
  onProgress({ stage: merge.processingStage, percent: 95, message: merge.message })
  return waitUntilCompleted(identifier, onProgress)
}

// 页面调用示例：<input id="videoFile" type="file">
document.querySelector('#videoFile')?.addEventListener('change', async event => {
  const file = event.target.files?.[0]
  if (!file) return
  try {
    const result = await uploadInChunks(file, progress => console.log('上传进度', progress))
    console.log('最终资源', result)
  } catch (error) {
    console.error('上传失败', error)
  }
})
