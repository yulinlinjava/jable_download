# JSON 资源导入

在管理后台「采集管理 → 采集资源」中点击「导入数据」。可以直接粘贴标准资源数组，也可以选择已保存的 JavaScript 格式化脚本，把任意 JSON 清洗为标准资源数组后预览并导入。每次支持 1–1000 条资源。

系统根据播放地址自动处理：地址对应服务端本地公开文件时直接关联并标记为已完成；直接粘贴或通过导入 API 提交的其他远程资源默认加入下载队列。列表任务调用同一导入流程时，是否自动入队由该列表任务的 `auto_download_enabled` 控制。资源也可以随时通过单条、批量或“全部下载”手动入队；实际执行仍受全局调度开关和并发数控制。

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
    "published_at": "2026-09-18 16:37:48",
    "actors": ["演员甲"],
    "tags": ["标签一"],
    "m3u8_url": "https://cdn.example.com/demo-001/index.m3u8"
  }
]
```

示例地址为占位地址，导入前请替换。

- `source`、`id`、`title` 必填，并至少提供 `m3u8_url` 或 `video_url`。如果 `url` 本身以 `.m3u8` 结尾，也可直接作为播放地址。`source`、`id` 支持 1–100 位英文字母、数字、下划线或连字符，不允许 Windows 保留名称。来源统一转为小写，可用 `jable`、`123av`、`external` 或自定义来源。
- 其余字段可省略；演员和标签为字符串数组，省略时保存为空数组。`published_at` 支持 `YYYY-MM-DD`，需要精确到秒时使用 `YYYY-MM-DD HH:mm:ss`；也兼容相同结构的 `T` 分隔符并统一保存为空格。时长使用字符串。可选 `vtt_url` 用于进度缩略图，`preview_m3u8_url`、`preview5_m3u8_url` 分别用于 3 分钟和 5 分钟 HLS 预览。VTT 中引用的图片会以 `vtt-preview-001.*` 等名称保存在视频同一目录，并改写为相对路径。
- 播放地址可以是 HTTP/HTTPS 地址、本地公开路径或第三方相对路径。第三方相对路径按source在来源管理中配置域名。系统安全映射 `/uploads/`、`/jable/`、`/123/`、`/external/` 到下载目录并确认文件真实存在；存在时不复制、不下载，直接标记完成。完整地址只有主机匹配本系统公开地址时才检查本地文件，不会把其他域名的相同路径误认为本地资源。其他地址自动排队并沿用现有 HLS 分片、校验、断点恢复与本地播放流程；下载器仍会拒绝非公网远程目标。
- `url` 为可选详情页地址，也用于下载请求的 Referer。导入资源不再抓取详情页，直接使用 `m3u8_url`。
- 多章节资源可提供 `chapters` 数组，每项包含 `title`、可选 `duration`、必填 `m3u8_url`，以及可选的 `vtt_url`、`preview_m3u8_url` 和 `preview5_m3u8_url`。系统将所有章节归入同一个番号任务，下载后为每章分别生成本地 3 分钟和 5 分钟预览；第一章节作为列表预览和默认播放内容。根级 `m3u8_url` 可省略，此时自动使用第一章节地址。
- 信息保存在任务表的 `metadata_json` 中；状态、进度、本地文件路径由系统维护，导入中的内部字段不会被采用。
- 番号忽略大小写。相同番号的旧任务处于 `pending` 或 `failed` 时，导入会覆盖播放链接和元数据，删除旧分片与进度后重新排队。`queued`、`downloading` 和 `completed` 任务保持原状并跳过覆盖。只有标题相同但番号不同时也会跳过，避免覆盖错误资源。失败项与有效项分别处理，结果显示数组条目序号及原因。
- 文件保存在运行服务的机器上：`DOWNLOAD_ROOT/123/<id>/`、`DOWNLOAD_ROOT/jable/<id>/`，自定义来源为 `DOWNLOAD_ROOT/external/<source>/<id>/`。视频为本地 HLS 清单及分片，附带封面等资源。
- 远程导入资源下载完成后，会保留已成功下载的来源附加资源，并使用第一章本地 `index.m3u8` 补齐缺少或下载失败的 `cover.jpg`、`poster.jpg`、`preview_image.mp4`、`preview.vtt` 和雪碧图。每个章节还会生成自己的 `preview.m3u8` 和 `preview5.m3u8`：它们直接引用该章现有分片，从开头分别选择不超过 180 秒和 300 秒的完整分片，不复制分片、不转码。生成失败只记录在任务 `warnings`，不影响视频下载完成状态。

## API

`POST /api/collection-items/import`

请求使用 `Content-Type: application/json`，请求体直接为包含source的数组，或顶层包含source的第三方原始对象；系统自动匹配格式化脚本，无需选择脚本或额外参数。也兼容显式指定 `?source=partner_a`；`format=false` 可跳过格式化；启用 API Key 时需提供 `X-API-Key`。直接导入的远程资源默认加入下载队列；旧的 `download` 查询参数会被忽略。列表采集使用任务自身的 `auto_download_enabled`，不读取来源配置决定是否下载。

响应 `data` 包含 `total`、`created`、`local_linked`、`queued`、`saved_only`、`skipped`、`failed` 及逐条 `results`。逐条结果包含从 1 开始的 `index`、`status`（`created` / `skipped` / `failed`）、`message`；成功或重复项还包含 `task_id`。

非法JSON、格式化失败或格式化结果不是1–1000条数组时返回HTTP400。数组中单条数据错误时返回 HTTP 200，并在 `results` 中说明该条失败原因。

## 格式化与签名统一管理

在「工具 → 来源管理」配置同一source的图片域名、视频域名、格式化脚本和签名脚本。来源管理不控制资源是否下载。导入窗口不选择来源或脚本，直接提交原始JSON；导入接口内部根据数组每条数据的source自动匹配规则。顶层含source的原始对象也会自动识别来源。使用同步 `function format(raw, context, config)` 返回标准资源数组，服务端执行最多3秒。后台和第三方导入接口共用规则。

后台可以通过校验接口预览格式化结果，确认导入时仍提交原始JSON。正式导入接口内部自动匹配来源、执行格式化、校验并写入数据库；未配置格式化脚本或配置停用时直接校验标准数据。接口默认启用自动格式化，`format=false`只用于已经标准化的内部节点同步等兼容场景。

数据库保留格式化后的原始链接；需要签名的图片、视频在预览或下载请求发出前才签名。下载完成后使用本地封面、预览和视频文件。详细配置、脚本案例与接口参数见 [来源管理](download-signers.md)。

直接 JSON 导入的有效远程资源默认排队；列表采集关闭 `auto_download_enabled` 时，本次资源只保存为 `pending`，开启后自动排队。来源配置不会改变已有任务状态。无论资源来自哪种方式，单条、批量和全部下载操作都可以把 `pending` 或 `failed` 资源手动加入队列。
