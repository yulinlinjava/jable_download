# 资源导出与第三方同步

在管理后台「资源管理」中选择一条或多条资源：

- 「导出 JSON」直接下载 UTF-8 JSON 数组。
- 「第三方网站」用于管理多个目标接口、鉴权、批量数和超时。同步时选择一个已启用的网站，可以同步当前勾选的资源，也可以同步资源管理中的全部资源。

导出及同步使用相同的数据结构：

```json
[
  {
    "source": "123av",
    "id": "demo-001",
    "title": "示例资源一",
    "url": "https://example.com/v/demo-001",
    "cover": "https://media.example.com/123/demo-001/cover.jpg",
    "poster": "https://media.example.com/123/demo-001/poster.jpg",
    "preview_image": "https://media.example.com/123/demo-001/preview_image.jpg",
    "vtt_url": "https://media.example.com/123/demo-001/preview.vtt",
    "preview_m3u8_url": "https://media.example.com/123/demo-001/preview.m3u8",
    "preview5_m3u8_url": "https://media.example.com/123/demo-001/preview5.m3u8",
    "duration": "01:30:00",
    "published_at": "2026-09-18",
    "actors": ["演员甲"],
    "tags": ["标签一"],
    "m3u8_url": "https://media.example.com/123/demo-001/index.m3u8"
  }
]
```

下载完成的资源优先导出本地公开地址，尚未下载的资源导出原始远程地址。缺少的可选字段为 `null` 或空数组。`preview_m3u8_url` 和 `preview5_m3u8_url` 分别是最多 3 分钟和 5 分钟的短时 HLS 预览，管理页封面悬浮播放会优先使用 3 分钟地址。

VTT 文件保存为资源目录中的 `preview.vtt`，其引用图片使用 `vtt-preview-001.*`、`vtt-preview-002.*` 等名称，与 `index.m3u8` 和视频分片位于同一目录。VTT 内使用相对文件名，第三方只需读取 `vtt_url` 即可继续加载这些图片。

## 同步协议

系统向所选网站的接口发送 `POST application/json` 请求，每批 1–100 条：

```json
{
  "request_id": "唯一批次编号",
  "resources": []
}
```

第三方可以只返回 HTTP 2xx，也可以返回逐条结果：

```json
{
  "success": true,
  "results": [
    {
      "source": "123av",
      "id": "demo-001",
      "status": "success",
      "remote_id": "98765",
      "message": "保存成功"
    }
  ]
}
```

`status` 为 `failed` 或 `error` 时，该条记为失败；未返回逐条结果的 HTTP 2xx 请求按整批成功处理。HTTP 4xx 直接失败，连接错误和 HTTP 5xx 最多尝试三次。响应最大读取 1 MB，接口重定向不会自动跟随。

支持无鉴权、Bearer Token 和自定义请求头的 API Key。密钥使用服务本地随机密钥进行 AES-GCM 加密，管理接口只返回是否已配置，不返回明文。加密密钥保存在数据库同目录的 `tasks.sync.key`，备份或迁移数据库时应同时保留该文件。

第三方同步接口支持 HTTP/HTTPS、局域网地址、localhost 和自定义端口。运行服务的容器或主机必须能够访问所填写的地址；在 Docker 中填写 `localhost` 时，它指向 API 容器自身。

## API

- `POST /api/collection-items/export`，请求体为 `{"ids":["任务 ID"]}`，返回 JSON 文件。
- `GET /api/resource-sync/sites`，查询第三方网站。
- `POST /api/resource-sync/sites`，添加第三方网站。
- `PUT /api/resource-sync/sites/<site_id>`，修改第三方网站；`secret` 为空时保留已配置密钥。
- `DELETE /api/resource-sync/sites/<site_id>`，删除网站配置，历史同步记录仍保留。
- `POST /api/resource-sync/execute`，同步选中资源时请求体为 `{"site_id":"网站 ID","ids":["任务 ID"],"all":false}`；同步全部资源时使用 `{"site_id":"网站 ID","ids":[],"all":true}`。
- `GET /api/resource-sync/runs`，查询最近同步记录；可传 `site_id` 筛选目标网站。
- `POST /api/resource-sync/runs/<run_id>/retry`，使用原目标网站重新同步该记录中的失败资源。
