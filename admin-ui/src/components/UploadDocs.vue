<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { DocumentCopy } from '@element-plus/icons-vue'
import chunkUploadExample from '../examples/chunk-upload.js?raw'
import vueUploadExample from '../examples/chunk-upload-vue.vue?raw'
import javaProxyExample from '../examples/upload-proxy.java?raw'
import phpProxyExample from '../examples/upload-proxy.php?raw'

const endpoints = [
  { method: 'POST', path: '/api/bigFileUploads（PHP 兼容入口）', description: '兼容旧 Vue 的 chunk、filename、chunkNumber、totalChunks 协议；最后一个分片自动提交 merge' },
  { method: 'GET', path: '/upload/chunk?identifier=...', description: '查询已上传分片、后台处理状态和最终资源地址' },
  { method: 'POST', path: '/upload/chunk', description: 'multipart/form-data 上传一个分片，分片编号从 1 开始' },
  { method: 'POST', path: '/upload/merge', description: '提交异步合并任务；视频会继续生成 HLS、封面和预览资源' },
  { method: 'POST', path: '/upload/file', description: 'multipart/form-data 直接上传一个完整文件' },
  { method: 'POST', path: '/upload/deleteByPath', description: '按公开 URL 或本地公开路径删除资源' },
  { method: 'DELETE', path: '/upload/{identifier}', description: '按上传 ID 删除文件、临时分片和记录' },
]

async function copyExample(content = vueUploadExample, label = 'Vue 3') {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(content)
    } else {
      const textarea = document.createElement('textarea')
      textarea.value = content
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.select()
      document.execCommand('copy')
      textarea.remove()
    }
    ElMessage.success(`${label} 示例已复制`)
  } catch {
    ElMessage.error('复制失败，请手动选择代码复制')
  }
}
</script>

<template>
  <div class="upload-docs">
    <el-card shadow="never" class="panel">
      <template #header><div class="panel-head"><div><h2>分片上传对接文档</h2><p>适用于浏览器大文件上传、服务器安全转发、断点续传和异步视频处理。</p></div><el-button type="primary" :icon="DocumentCopy" @click="copyExample()">复制 Vue 3 示例</el-button></div></template>

      <el-alert type="info" :closable="false" show-icon>
        <template #title>Vue 直连示例会在每个请求中携带 <code>X-API-Key</code>；Java/PHP 示例用于需要隐藏密钥的服务器转发场景。</template>
      </el-alert>

      <section class="doc-section doc-notes">
        <h3>服务器转发架构</h3>
        <ul>
          <li>Vue 直连时配置上传服务地址和 <code>X-API-Key</code>，浏览器直接请求 <code>/upload/*</code>。</li>
          <li>Java/PHP 接收浏览器参数和文件流，向真正的上传服务补充 <code>X-API-Key</code>。PHP 还提供 <code>/api/bigFileUploads</code> 兼容入口，可直接接收旧 Vue 客户端的字段；它使用首片前 16 KiB、文件名和总分片数生成 16 位 ID，并在最后一片后自动提交合并。</li>
          <li>采用服务器转发时，客户端仍负责计算 <code>identifier</code>；转发层不重新计算，查询、上传和合并始终使用同一个值。</li>
          <li>Java 示例使用临时文件和 <code>FileSystemResource</code>，文件不会整体进入 Java 堆，也不会发生输入流被预读后无法再次读取。</li>
        </ul>
      </section>

      <section class="doc-section">
        <h3>对接流程</h3>
        <el-steps :active="4" finish-status="success" align-center>
          <el-step title="生成 ID" description="首尾各 16 KiB + 大小，生成 16 位 ID"/>
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
          <li>HTTPS/localhost 使用文件大小和首尾各 16 KiB 计算 SHA-256，并截取 16 位；普通 HTTP 自动使用文件元信息的 FNV-1a 64 位结果。</li>
          <li>每个分片都必须传入 <code>totalChunks</code>，分片文件无需携带原始文件名。</li>
          <li>重复上传相同 <code>chunkNumber</code> 会原子覆盖，可安全重试。</li>
          <li><code>/upload/merge</code> 必须传入 <code>identifier</code> 和 <code>fileName</code>；服务端从上传记录读取总分片数，并根据文件名识别资源类型。</li>
          <li>处理状态依次为 <code>merging</code>、<code>queued</code>、<code>probing</code>、按需进入 <code>compressing</code>（否则为 <code>segmenting</code>）、<code>generating_cover</code>、<code>generating_preview</code>、<code>generating_vtt</code>、<code>completed</code>；失败或取消时为 <code>failed</code>、<code>cancelled</code>。</li>
          <li>管理后台“FFmpeg 任务”Tab 可设置处理并发数；设置为 <code>0</code> 会暂停新任务调度。</li>
          <li>“压缩配置”对话框保存的参数会应用于等待中和后续任务；正在运行的任务不会被中途修改。</li>
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
          <el-descriptions-item label="duration / durationText">视频时长秒数及格式化时长</el-descriptions-item>
          <el-descriptions-item label="width / height">原视频宽度和高度</el-descriptions-item>
          <el-descriptions-item label="videoCodec / audioCodec">原视频和音频编码</el-descriptions-item>
          <el-descriptions-item label="bitRate">检测到的视频码率，单位 bps</el-descriptions-item>
          <el-descriptions-item label="originalSize / processedSize">原文件及最终 TS 分片总字节数</el-descriptions-item>
          <el-descriptions-item label="compressed / compressionRatio">是否转码及体积减少百分比</el-descriptions-item>
        </el-descriptions>
      </section>

      <section class="doc-section">
        <div class="code-heading"><div><h3>完整接入示例</h3><p>Vue提供直连上传示例；Java与PHP提供可选的服务器转发方案。</p></div></div>
        <el-tabs type="border-card" class="upload-example-tabs">
          <el-tab-pane label="Vue 3 客户端">
            <div class="code-heading"><p>直接配置API地址和 <code>X-API-Key</code>，包含文件标识、断点续传、三次重试、进度和状态轮询。</p><el-button :icon="DocumentCopy" @click="copyExample(vueUploadExample, 'Vue 3')">复制代码</el-button></div>
            <pre class="code-block"><code>{{vueUploadExample}}</code></pre>
          </el-tab-pane>
          <el-tab-pane label="Java 转发">
            <div class="code-heading"><p>Spring Boot 3.2+，服务端配置目标地址和密钥，multipart 使用可重复读取的临时文件。</p><el-button :icon="DocumentCopy" @click="copyExample(javaProxyExample, 'Java')">复制代码</el-button></div>
            <pre class="code-block"><code>{{javaProxyExample}}</code></pre>
          </el-tab-pane>
          <el-tab-pane label="PHP 转发">
            <div class="code-heading"><p>原生 PHP cURL，可放在 PHP-FPM 站点中；同时兼容标准 <code>/upload/*</code> 和旧客户端 <code>/api/bigFileUploads</code>。</p><el-button :icon="DocumentCopy" @click="copyExample(phpProxyExample, 'PHP')">复制代码</el-button></div>
            <pre class="code-block"><code>{{phpProxyExample}}</code></pre>
          </el-tab-pane>
          <el-tab-pane label="浏览器直连">
            <div class="code-heading"><p>仅适合受信环境；该方式需要在浏览器配置 <code>X-API-Key</code>。</p><el-button :icon="DocumentCopy" @click="copyExample(chunkUploadExample, 'JavaScript 直连')">复制代码</el-button></div>
            <pre class="code-block"><code>{{chunkUploadExample}}</code></pre>
          </el-tab-pane>
        </el-tabs>
      </section>
    </el-card>
  </div>
</template>
