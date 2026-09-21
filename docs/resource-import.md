# JSON 资源导入

在管理后台「资源管理」中点击「导入数据」。可以直接粘贴标准资源数组，也可以选择已保存的 JavaScript 格式化脚本，把任意 JSON 清洗为标准资源数组后预览并导入。每次支持 1–1000 条资源。

系统根据播放地址自动处理：地址对应服务端本地公开文件时直接关联并标记为已完成；其他远程资源自动加入下载队列，实际执行仍受全局调度开关和并发数控制。

```json
[
  {
    "source": "123av",
    "id": "demo-001",
    "title": "示例资源一",
    "url": "https://example.com/v/demo-001",
    "cover": "https://cdn.example.com/demo-001/cover.jpg",
    "poster": null,
    "preview_image": null,
    "vtt_url": "https://cdn.example.com/demo-001/preview.vtt",
    "preview_m3u8_url": null,
    "preview5_m3u8_url": null,
    "duration": "01:30:00",
    "published_at": "2026-09-18",
    "actors": ["演员甲"],
    "tags": ["标签一"],
    "m3u8_url": "https://cdn.example.com/demo-001/index.m3u8"
  }
]
```

示例地址为占位地址，导入前请替换。

- `source`、`id`、`title` 必填，并至少提供 `m3u8_url` 或 `video_url`。如果 `url` 本身以 `.m3u8` 结尾，也可直接作为播放地址。`source`、`id` 支持 1–100 位英文字母、数字、下划线或连字符，不允许 Windows 保留名称。来源统一转为小写，可用 `jable`、`123av`、`external` 或自定义来源。
- 其余字段可省略；演员和标签为字符串数组，省略时保存为空数组。日期使用有效的 `YYYY-MM-DD`，时长使用字符串。可选 `vtt_url` 用于进度缩略图，`preview_m3u8_url`、`preview5_m3u8_url` 分别用于 3 分钟和 5 分钟 HLS 预览。VTT 中引用的图片会以 `vtt-preview-001.*` 等名称保存在视频同一目录，并改写为相对路径。
- 播放地址可以是 HTTP/HTTPS 地址，也可以是以 `/` 开头的本地公开路径。系统安全映射 `/uploads/`、`/jable/`、`/123/`、`/external/` 到下载目录并确认文件真实存在；存在时不复制、不下载，直接标记完成。使用本地前缀但文件不存在时该条导入失败。其他地址自动排队并沿用现有 HLS 分片、校验、断点恢复与本地播放流程；下载器仍会拒绝非公网远程目标。
- `url` 为可选详情页地址，也用于下载请求的 Referer。导入资源不再抓取详情页，直接使用 `m3u8_url`。
- 信息保存在任务表的 `metadata_json` 中；状态、进度、本地文件路径由系统维护，导入中的内部字段不会被采用。
- 沿用数据库全局去重规则：番号忽略大小写，或规范化后的标题相同，均跳过；不会覆盖已有任务。失败项与有效项分别处理，结果显示数组条目序号及原因。修正后可重新提交，已导入条目会跳过。
- 文件保存在运行服务的机器上：`DOWNLOAD_ROOT/123/<id>/`、`DOWNLOAD_ROOT/jable/<id>/`，自定义来源为 `DOWNLOAD_ROOT/external/<source>/<id>/`。视频为本地 HLS 清单及分片，附带封面等资源。
- 远程导入资源下载完成后，会保留已成功下载的来源附加资源，并使用本地 `index.m3u8` 补齐缺少或下载失败的 `cover.jpg`、`poster.jpg`、`preview_image.mp4`、`preview.vtt` 和雪碧图。系统还会生成 `preview.m3u8` 和 `preview5.m3u8`：它们直接引用现有分片，从开头分别选择不超过 180 秒和 300 秒的完整分片，不复制分片、不转码。FFmpeg 生成失败只记录在任务 `warnings`，不影响视频下载完成状态。

## API

`POST /api/collection-items/import`

请求使用 `Content-Type: application/json`，请求体直接为上述数组；启用 API Key 时需提供 `X-API-Key`。旧的 `download` 查询参数会被忽略，导入策略完全由播放地址是否对应本地文件决定。

响应 `data` 包含 `total`、`created`、`local_linked`、`queued`、`skipped`、`failed` 及逐条 `results`。逐条结果包含从 1 开始的 `index`、`status`（`created` / `skipped` / `failed`）、`message`；成功或重复项还包含 `task_id`。

非法 JSON、非数组、空数组或超过 1000 条时返回 HTTP 400。数组中单条数据错误时返回 HTTP 200，并在 `results` 中说明该条失败原因。

## 格式化脚本

导入窗口的“使用格式化脚本”模式支持管理和选择多个脚本。脚本保存在同一个 SQLite 数据库中，但在管理页面的 Web Worker 中执行；执行超过 30 秒会被终止，不会阻塞页面。原始 JSON 可以是数组或任意对象，脚本必须声明 `async function format(raw, helpers)` 并返回 1–1000 条标准资源。

```js
async function format(raw, helpers) {
  const timestamp = helpers.now();
  const items = raw.data?.list || raw;
  return Promise.all(items.map(async (item) => {
    const sign = await helpers.hmacSha256('secret', `${item.playUrl}:${timestamp}`);
    return {
      source: 'external',
      id: item.code,
      title: item.name,
      m3u8_url: `${item.playUrl}?timestamp=${timestamp}&sign=${sign}`,
      cover: item.coverUrl || null,
      actors: item.actors || [],
      tags: item.tags || []
    };
  }));
}
```

脚本可以直接使用浏览器 `fetch` 和 Web Crypto；`helpers` 额外提供当前 Unix 秒数 `now()`、`sha256(value)`、`hmacSha256(secret, value)` 和 `base64Url(value)`。脚本、输入数据或所选脚本发生变化后，旧预览立即失效，必须重新执行。格式化结果先通过 `POST /api/collection-items/import/validate` 使用正式导入规则校验；确认后才提交正式导入接口。

脚本管理接口为 `GET/POST /api/import-scripts` 和 `PUT/DELETE /api/import-scripts/{id}`，全部需要 `X-API-Key`。直接导入模式不会执行任何脚本，可以不经过预览直接提交原始标准数组。

如果某个来源需要短期播放签名，格式化脚本只需输出正确的 `source` 和原始资源 URL。下载器会根据“签名脚本”Tab 中相同的 `source` 自动选择下载签名函数，在实际文件请求前生成临时 URL；导入阶段无需生成或保存签名。
