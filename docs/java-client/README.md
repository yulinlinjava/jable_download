# Java 上传接口代理

将 `ChunkUploadService.java` 复制到 Java 项目的
`com.cloud.admin.upload` 包中，替换原来的本地上传服务。现有
`ChunkUploadController` 不需要修改。

该实现直接注入 `com.yulinlin.data.core.http.HttpRequestClient`，因此项目需要已经引入
`com.yulinlin:starter`（或其他包含 `core.http` 的模块）。JSON、查询参数和 multipart
请求都复用统一 HTTP 客户端。转发前使用临时文件包装为可重复读取的
`FileSystemResource`，避免 Spring 提前读取 `InputStreamResource` 导致上传失败；
大文件不会一次性读入 Java 堆，请确保系统临时目录具有足够空间。

把 `application-upload.yml` 的内容合并到 Java 项目的配置文件，并通过环境变量设置密钥：

```bash
export JABLE_UPLOAD_API_KEY='服务端的 JABLE_API_KEY'
```

该实现会将现有方法转发到以下接口：

- `GET /upload/chunk`：查询已上传分片和后台处理状态。
- `POST /upload/chunk`：上传单个分片，`chunkNumber` 从 1 开始，分片不需要原始文件名。
- `POST /upload/merge`：传入 `identifier` 和 `fileName`，提交后台合并与视频处理，立即返回最终资源地址。
- `POST /upload/file`：流式转发完整文件。
- `POST /upload/deleteByPath`：按公开 URL 或 `/uploads/...` 路径删除资源。

所有请求自动携带 `X-API-Key`。视频合并后返回 `index.m3u8`，图片和其他文件返回其公开文件 URL。
Java 服务不再保存分片、不再维护上传任务表，也不再执行 FFmpeg。

`remote-upload.request-timeout-minutes` 是上传接口的单次覆盖超时，默认 120 分钟。
普通 HTTP 请求仍使用 `yulinlin.http.timeout` 的全局配置。
