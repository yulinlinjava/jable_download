<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { DocumentCopy } from '@element-plus/icons-vue'
import chunkUploadExample from '../examples/chunk-upload.js?raw'

const endpoints = [
  { method: 'GET', path: '/upload/chunk?identifier=...', description: '查询已上传分片、后台处理状态和最终资源地址' },
  { method: 'POST', path: '/upload/chunk', description: 'multipart/form-data 上传一个分片，分片编号从 1 开始' },
  { method: 'POST', path: '/upload/merge', description: '提交异步合并任务；视频会继续生成 HLS、封面和预览资源' },
]

async function copyExample() {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(chunkUploadExample)
    } else {
      const textarea = document.createElement('textarea')
      textarea.value = chunkUploadExample
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.select()
      document.execCommand('copy')
      textarea.remove()
    }
    ElMessage.success('JavaScript 示例已复制')
  } catch {
    ElMessage.error('复制失败，请手动选择代码复制')
  }
}
</script>

<template>
  <div class="upload-docs">
    <el-card shadow="never" class="panel">
      <template #header><div class="panel-head"><div><h2>分片上传对接文档</h2><p>适用于浏览器大文件上传、断点续传和异步视频处理。</p></div><el-button type="primary" :icon="DocumentCopy" @click="copyExample">复制 JavaScript 示例</el-button></div></template>

      <el-alert type="info" :closable="false" show-icon>
        <template #title>所有请求都必须携带 <code>X-API-Key</code>。示例中的 API 地址和密钥需要替换为实际配置。</template>
      </el-alert>

      <section class="doc-section">
        <h3>对接流程</h3>
        <el-steps :active="4" finish-status="success" align-center>
          <el-step title="生成 ID" description="根据文件元信息生成稳定的 16 位 ID"/>
          <el-step title="查询分片" description="获取 uploadedChunks，跳过已经存在的分片"/>
          <el-step title="上传分片" description="按 chunkNumber 从 1 开始顺序上传"/>
          <el-step title="合并与轮询" description="提交 merge 后轮询到 completed"/>
        </el-steps>
      </section>

      <section class="doc-section">
        <h3>接口</h3>
        <el-table :data="endpoints" border>
          <el-table-column label="方法" width="90"><template #default="{row}"><el-tag :type="row.method==='GET'?'success':'primary'">{{row.method}}</el-tag></template></el-table-column>
          <el-table-column prop="path" label="地址" min-width="330"><template #default="{row}"><code>{{row.path}}</code></template></el-table-column>
          <el-table-column prop="description" label="说明" min-width="420"/>
        </el-table>
      </section>

      <section class="doc-section doc-notes">
        <h3>关键规则</h3>
        <ul>
          <li>推荐分片大小为 10 MiB；服务端默认允许单分片最大 50 MiB。</li>
          <li><code>identifier</code> 允许字母、数字、下划线和连字符，长度为 1–128。</li>
          <li>重复上传相同 <code>chunkNumber</code> 会原子覆盖，可安全重试。</li>
          <li><code>/upload/merge</code> 只提交后台任务，收到响应后仍需轮询查询接口。</li>
          <li>处理状态依次为 <code>merging</code>、<code>segmenting</code>、<code>generating_cover</code>、<code>generating_preview</code>、<code>generating_vtt</code>、<code>completed</code>；失败时为 <code>failed</code>。</li>
          <li>视频完成后返回 <code>hlsPath</code>、<code>coverPath</code>、<code>previewPath</code> 和 <code>vttPath</code>。</li>
        </ul>
      </section>

      <section class="doc-section">
        <h3>完成响应主要字段</h3>
        <el-descriptions :column="2" border>
          <el-descriptions-item label="identifier">上传会话和资源 ID</el-descriptions-item>
          <el-descriptions-item label="path">最终主文件地址；视频为 M3U8</el-descriptions-item>
          <el-descriptions-item label="hlsPath">视频 HLS 播放地址</el-descriptions-item>
          <el-descriptions-item label="coverPath">列表默认封面图片</el-descriptions-item>
          <el-descriptions-item label="previewPath">列表鼠标悬浮预览 MP4</el-descriptions-item>
          <el-descriptions-item label="vttPath">播放器进度缩略图 VTT</el-descriptions-item>
        </el-descriptions>
      </section>

      <section class="doc-section">
        <div class="code-heading"><div><h3>浏览器 JavaScript 完整示例</h3><p>不读取完整大文件计算哈希，支持刷新或失败后继续上传。</p></div><el-button :icon="DocumentCopy" @click="copyExample">复制代码</el-button></div>
        <pre class="code-block"><code>{{chunkUploadExample}}</code></pre>
      </section>
    </el-card>
  </div>
</template>
