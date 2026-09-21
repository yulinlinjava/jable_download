# JableTVDownload

## 下載JableTV好幫手

## HTTP API

### Vue 3 管理后台

资源管理支持粘贴 JSON 批量导入：可以直接导入标准数组，也可以选择保存在服务端的 JavaScript 格式化脚本，在浏览器 Worker 中完成字段清洗、链接签名和结果预览；播放地址对应本地公开文件时直接关联，其他远程资源自动加入下载队列。也可导出选中资源并同步到第三方 HTTP 接口。字段示例、脚本格式、去重规则及 API 见 [JSON 资源导入说明](docs/resource-import.md) 和 [资源导出与第三方同步](docs/resource-export-sync.md)。

启动 API 后打开 `http://localhost:8000/admin`。管理页使用 Vue 3、TypeScript、Vite、Element Plus、Pinia、Vue Router、Plyr 和 Hls.js，提供：

- 新建、编辑、删除和手动执行列表采集任务；支持 Jable 最近更新页与 123AV FC2 列表页。
- 列表采集任务支持当前表格多选批量执行；每个任务可独立开启“每天 18:00”定时采集，统一使用北京时间。同一天只调度一次，服务在 18:00 后启动时会补执行当天尚未运行的任务。
- 管理页分为“列表采集”和“资源管理”两个 Tab。资源管理把采集记录与下载任务合并为一张表，每个视频只显示一条，并集中展示状态、进度、文件大小、错误、播放和删除操作；没有采集记录的直接下载任务也会显示。
- 列表采集只负责保存当前页全部视频信息；有标题时按 NFKC 规范化、合并空白并忽略大小写后去重，无标题时按“来源站点 + 视频标识”去重。采集请求不会等待视频下载。
- 采集资源支持后端分页（20/50/100 条）、标题模糊搜索、来源与下载状态筛选，并可手动触发单条下载。
- 采集资源支持当前页多选批量下载；“全部下载”会把所有分页中的待下载资源一次性加入下载任务队列并立即返回。入队不会同步下载，实际执行由全局调度开关和并发数控制。
- 选中的资源可导出为统一 JSON 文件，字段包含本地或远程 `m3u8_url`、`vtt_url`、封面、演员和标签；支持配置一个第三方接口，按批发送、记录结果并重试失败资源。
- 全局自动调度默认开启，下载并发数默认 1、可设置为 1—10。关闭后已排队任务继续保留，已经运行的任务继续完成；重新开启后继续消费队列，并发降低时不会强制中断运行中的任务。
- 独立“签名脚本”Tab 可按资源 `source` 配置服务端 JavaScript 签名函数。数据库只保存原始资源地址；M3U8、TS、密钥、封面与 VTT 请求发出前才动态签名，临时 URL 不落库。配置说明见 [下载签名脚本](docs/download-signers.md)。
- 顶部“实时下载网速”由全局共享的 `FileDownloader` 在内存中汇总所有下载线程收到的字节，按最近 5 秒滑动窗口计算，不依赖数据库进度落库；多任务并发时显示总速度，并在 B/s、KB/s、MB/s、GB/s 之间自动换算。最近 5 秒没有收到数据或服务刚启动尚无采样时显示 `0 B/s`。
- 下载队列、分片进度、文件大小、错误和警告自动刷新。
- 管理后台只分页请求当前资源页，不再预取 500 条下载任务；顶部数量通过轻量调度状态接口统计，播放优先使用当前资源行中的下载详情。
- 资源管理表格显示 240×135 封面。已加入下载的资源优先使用本地播放器封面或列表封面，尚未保存本地图片时使用采集记录中的远程封面，失败时显示占位图。鼠标悬浮时优先播放本地最多 3 分钟的 `preview.m3u8`，不可用时回退到 `preview_image.mp4/webm` 或来源预览地址，移出后停止并释放播放器。
- 下载任务按视频 code（`video_id`，忽略大小写）跨来源防重。后台“按番号去重”可清理历史重复数据：每组保留状态最好、更新时间最新的一条，并删除其他任务及其资源。
- 点击资源管理中已完成的资源会弹出 Plyr 播放器；Hls.js 播放本地 `index.m3u8`，并加载本地播放器封面、远程采集封面或内置占位封面，以及 `preview.vtt` 进度缩略图。删除本地文件时保留采集记录并恢复为待下载；没有采集记录的直接下载任务会从资源列表移除。启用 API Key 后，播放器会自动申请 6 小时有效的任务级媒体令牌供缩略图使用。
- 管理页打开时必须输入 `X-API-Key` 并通过服务端验证；密钥只保存在当前浏览器的 `localStorage`。右上角钥匙按钮可重新验证密钥。
- 管理页提供独立的“上传文件”Tab，使用后端分页、文件名搜索、类型与状态筛选展示全部新上传记录，并支持预览、单条删除和批量删除。上传对话框把文件按 10 MiB 分片顺序上传，并在重试时查询和跳过已有分片；完成后可直接预览图片、音频、PDF 和视频，其他文件可下载。“分片上传文档”Tab 提供鉴权说明、接口流程以及可复制的浏览器原生 JavaScript 完整案例。上传视频由服务端 FFmpeg 使用流复制生成 3 秒 VOD HLS，并自动生成列表封面 `cover.jpg`、播放器封面 `poster.jpg`、多时段拼接的悬浮预览 `preview_image.mp4`、进度预览 `preview.vtt` 及雪碧图。全部处理结束后删除合并产生的原视频。

采集任务、采集资源、调度设置和下载任务共同持久化在 `downloads/tasks.sqlite3`。重启服务后全部数据和并发设置仍然存在；中断的采集执行会回到空闲状态，之后可手动再次执行。当前版本每次只采集传入链接对应的当前页，不自动翻页。采集完成的资源先保持待下载，用户通过单条、批量或“全部下载”加入下载任务队列。

本地前端开发：

```bash
cd admin-ui
npm install
npm run dev
```

生产构建由 Dockerfile 的 Node 阶段自动完成，也可在 `admin-ui` 目录运行 `npm run build`。类型检查命令是 `npm run typecheck`。

### 持久化下载任务

```bash
curl -X POST "http://localhost:8000/api/download" -H "Content-Type: application/json" -d '{"url":"https://123av.com/cn/v/fc2-ppv-4975150"}'
curl "http://localhost:8000/api/download/<task_id>"
curl -X DELETE "http://localhost:8000/api/download/<task_id>"
```

两个接口都返回 `code/message/data`。`data` 包含 `task_id`、`status`、`progress`（0—100）、`downloaded_segments`、`total_segments`、`downloaded_bytes`、`save_path`、`playlist_path`、`error`、`warnings` 和时间戳。
`DELETE /api/download/{task_id}` 会先停止指定任务，再删除任务数据库记录及 `downloads/<站点>/<视频标识>/` 整个资源目录（视频分片、m3u8、封面、预览、VTT 和元数据）。删除不可恢复；查询或再次删除该任务返回 404。删除其他任务或共享的 `tasks.sqlite3` 不受影响。
状态：`queued` 排队、`downloading` 下载中、`completed` 完成、`failed` 失败。解析清单前总分片数和进度为 0；分片全部完成但尚在保存附加资源时进度最多 99.9%。

按站点和视频标识去重。同一视频再次 POST 返回已有任务；失败任务再次 POST 会保留进度重新排队。启动时自动恢复排队和中断中的任务，已完成任务不会重新下载视频，但再次 POST 会在保持 `completed` 和 100% 进度的同时检查并补齐图片/VTT。任务存放于 `downloads/tasks.sqlite3`，资源在 `downloads/<站点>/<视频标识>/`；123AV 的磁盘站点目录固定使用 `123`，例如 `downloads/123/fc2-ppv-4975150/`，Jable 仍使用 `downloads/jable/<视频标识>/`。单个 API 进程内部使用动态并发工作池，请勿启动多个 uvicorn worker 共用同一下载目录。

TS 分片与 M3U8 保存在同一视频目录。单轨视频使用 `0000.ts`、`0001.ts`；存在独立音轨时使用 `video-0000.ts`、`audio-0000.ts` 防止重名。下载器不按来源 `.css`/`.js` 等扩展名过滤，也不因扩展名转码。本地 M3U8 直接使用同目录相对路径；AES-128 密钥、初始化片段、分片字节范围会同步处理。fMP4 来源保持原内容和初始化引用，仅按要求将分片后缀改为 `.ts`，这不表示将内容转换成 MPEG-TS。
主清单选择最高分辨率（同分辨率优先最高码率），有独立音轨时保存默认音轨。只支持已结束的点播，不支持直播、DRM/非 AES-128 加密、带字节范围的初始化片段和多层主清单。

M3U8、视频分片、AES 密钥、封面、悬停预览、VTT 和进度缩略图统一经过全局共享的 `FileDownloader`；来源浏览器会话只作为传输层传入。该对象统一处理取消检查、大小限制、内容校验、最多 3 次重试、实时网速统计、SHA-256 和 `.part` 原子落盘。SQLite 的任务进度仍按每新增 10 个分片或最多 5 秒批量更新，避免高频写库。恢复时验证文件，完整分片跳过，仅下载缺失或损坏分片；任务最多重新解析来源 3 次，以刷新过期播放链接。刷新后若分片结构变化则报错并保留旧文件，避免拼接不同版本。

资源下载完成后，任务表中的 `cover_url`、`preview_url`、`vtt_url`、`m3u8_url`、`preview_m3u8_url` 和 `preview5_m3u8_url` 会替换为公开静态地址；远程来源继续保存在 `metadata_json` 中供恢复下载使用。`PUBLIC_BASE_URL` 控制公开地址前缀，默认 `http://localhost:8000`。例如 `downloads/jable/abc/cover.jpg` 对应 `http://localhost:8000/jable/abc/cover.jpg`，`downloads/123/fc2-ppv-4975150/index.m3u8` 对应 `http://localhost:8000/123/fc2-ppv-4975150/index.m3u8`。第三方导入的视频下载完成后，会用本地 `index.m3u8` 补齐缺失的封面、悬浮预览和 VTT，并生成直接复用现有分片、最长 180 秒的 `preview.m3u8` 以及最长 300 秒的 `preview5.m3u8`；这些附加资源失败只写入警告，不改变视频的完成状态。管理页和第三方系统可以直接使用这些完整地址。
视频元数据直接保存在 `tasks` 表，不再生成 `metadata.json`。视频目录保存列表封面 `cover.*`、列表悬停预览 `preview_image.*`、播放器封面 `poster.*`、VTT 及其引用的全部进度拼图；本地 VTT 改为相对路径。进度拼图直接放在视频目录根部，第一张为 `preview.jpg`，多张时依次为 `preview-002.jpg`、`preview-003.jpg`（若来源实际不是 JPEG，则保留检测到的真实图片后缀），不创建 `thumbnails/` 子目录。下载任务只有详情链接时，会通过站内精确搜索补取该视频的列表卡片资源；找不到的资源与下载失败会记录在 `warnings`。资源根据文件签名使用真实后缀：123AV 的 `preview.png` 实际可能是 MP4 悬停视频，此时保存为 `preview_image.mp4`。图片、MP4、WebM 以外的伪装内容不会作为预览保存。附加资源失败不影响视频完成。下载依赖来源允许浏览器会话读取媒体，来源拒绝或跨域限制时任务会明确失败并保留进度。

Docker 已将 `./downloads` 挂载到 `/downloads`。服务公开映射 `downloads/jable/`、`downloads/123/`，以及上传目录 `downloads/uploads/video/`、`image/`、`other/`；公开地址分别使用 `/jable/`、`/123/`、`/uploads/video/`、`/uploads/image/`、`/uploads/other/`。上传临时分片 `downloads/uploads/chunks/` 不公开。静态资源响应允许跨域 GET、HEAD 和 Range 请求，便于第三方播放器直接使用。本地运行可通过 `DOWNLOAD_DIR` 设置下载目录，通过 `PUBLIC_BASE_URL` 设置导出链接的公开域名。数据库与文件必须一起保留，重建容器后才能继续恢复。

### 传入完整链接（Jable / 123AV）

`GET /api/list?url=<完整列表链接>` 自动识别站点。传入 `url` 时以链接中的分页为准；不传时保留原有 `/api/list?page=1` 行为。
来源地址采用宽松识别：HTTP/HTTPS 主机名只要包含 `123av` 或 `jable` 关键字即可，路径、端口和查询参数全部原样接受。例如 `https://123av.com/tw/makers/fc2?page=2&type=&sort=hot`。
列表 `data` 和每条视频增加 `source`（`jable` 或 `123av`）。

```bash
curl --get "http://localhost:8000/api/list" --data-urlencode "url=https://123av.com/cn/makers/fc2?sort=today&page=1"
curl --get "http://localhost:8000/api/detail" --data-urlencode "url=https://123av.com/cn/v/fc2-ppv-4975150"
```

Swagger 的 `url` 输入框直接粘贴完整链接。自己拼接请求时需 URL 编码，避免 `&` 被解释为 API 参数。
详情接受不同语言前缀下的 `/v/<id>`，返回同样的元信息及 `source`。123AV 的发布时间取“发布日期”，封面取播放器封面，演员与标签只读取详情区域。
123AV 嵌入播放器使用普通 Chromium 解析实际请求的 m3u8，当前支持 `javplayer.cc/e/<id>`；多集时返回第一集播放地址，不代理或下载视频。未知播放器返回明确解析错误。
第三方播放器可能需要数秒加载；媒体地址可能过期，过期后重新请求详情。

图片字段：`cover` 保留原有封面；列表新增 `preview_image`（页面提供的悬停预览图片，可能是多帧拼图）；详情新增 `poster`（播放器播放前显示的封面），并保留 `preview_image` 字段。
详情还返回 `has_vtt` 和 `vtt_url`。Jable 直接解析脚本的 `hasVtt`、`vttUrl`；123AV 在解析播放地址的同一浏览器会话内读取播放器请求的 `.vtt` 地址，最多额外等待 5 秒。获取到地址时 `has_vtt=true`；Jable 明确关闭预览时为 `false`；未能确认时为 `null`。地址缺失、非法或预览关闭时 `vtt_url=null`，不会影响已成功解析的视频返回。Jable 的地址可能以 `thumbvtt.ts` 结尾，但内容是 WebVTT。这里只返回索引链接，不额外读取索引、下载拼图或代理资源；链接可能过期或需要来源浏览器会话才能访问。
所有图片返回完整 HTTP(S) 地址，保留查询参数，不下载图片。来源页面没有对应资源时返回 `null`，不根据封面地址猜测预览图。预览拼图需按来源布局展示，不能保证直接作为动图播放。

Python 3.10+。保留原有命令行下载方式，API 仅返回数据，不下载视频。

```bash
pip install -r requirements.txt
python -m uvicorn api:app --host 0.0.0.0 --port 8000
```

Docker 启动 API（仅启动 api 服务）：

```bash
docker compose up -d --build api
```

交互式文档：`http://localhost:8000/docs`，OpenAPI：`/openapi.json`。

| 接口 | 参数 | 说明 |
| --- | --- | --- |
| `GET /api/list` | `url`：可选完整列表链接；`page`：正整数，默认 1 | 链接优先；不传链接时返回 Jable 最近更新 |
| `GET /api/detail` | `url`：必填视频详情地址 | 返回元信息及 m3u8 播放地址 |
| `GET /api/downloads` | `status`、`search`、`limit`、`offset` | 下载任务列表 |
| `POST /api/downloads/deduplicate` | 无 | 按视频 code 清理历史重复下载任务 |
| `GET /api/download/{task_id}/files/{path}` | 下载任务 ID、相对文件路径 | 播放本地 m3u8、TS、封面与 VTT，支持 Range |
| `GET/POST /api/collections` | POST：名称、列表链接 | 查询或创建采集任务 |
| `POST /api/collections/bulk-create` | `name`、`url_template`、`start_page`、`end_page`、`scheduled_enabled` | 用 `${page}` 模板批量新建最多 500 个任务，按标题或 URL 跳过重复项 |
| `PUT/DELETE /api/collections/{id}` | 采集任务 ID | 修改或删除空闲采集任务 |
| `POST /api/collections/{id}/execute` | 采集任务 ID | 异步触发一次列表采集 |
| `POST /api/collections/bulk-execute` | `ids`：最多 100 个采集任务 ID | 批量触发列表采集 |
| `GET /api/collection-items` | `page`、`page_size`、`title`、`source`、`status` | 分页查询统一资源列表，返回采集信息和关联下载详情，并包含未关联的直接下载任务 |
| `POST /api/collection-items/import` | 1–1000 条资源 JSON 数组 | 本地播放文件直接关联为已完成，远程资源自动加入下载队列 |
| `POST /api/collection-items/import/validate` | 1–1000 条资源 JSON 数组 | 使用正式导入规则预览校验、去重及本地资源判断，不写入数据 |
| `GET/POST /api/import-scripts` | POST：名称、说明、JavaScript 代码 | 查询或新增浏览器端导入格式化脚本 |
| `PUT/DELETE /api/import-scripts/{id}` | 脚本 ID | 修改或删除导入格式化脚本 |
| `GET/POST /api/download-signers` | POST：名称、source、代码、配置 | 查询或新增按来源匹配的下载签名脚本 |
| `PUT/DELETE /api/download-signers/{id}` | 签名脚本 ID | 修改或删除下载签名脚本 |
| `POST /api/download-signers/test` | 脚本、配置、测试 URL | 测试动态签名结果，不发送资源请求 |
| `POST /api/collection-items/{id}/download` | 采集资源 ID | 手动加入下载调度 |
| `POST /api/collection-items/bulk-download` | `ids`：最多 100 个采集资源 ID | 批量加入下载调度 |
| `POST /api/collection-items/bulk-delete` | `ids`：最多 100 个资源 ID | 批量停止任务并删除本地文件和数据库记录 |
| `POST /api/collection-items/download-all` | 无 | 将所有待下载采集资源批量加入下载任务队列 |
| `GET/PUT /api/settings/scheduler` | `enabled`、`concurrency` | 查询或更新全局调度开关与并发数 |
| `POST /upload/file` | `multipart/form-data` 的 `file` | 直接上传完整文件；按 96 位内容哈希去重并返回 `deduplicated` |
| `GET /upload/chunk` | `identifier` | 查询已上传分片和合并状态 |
| `POST /upload/chunk` | `identifier`、`chunkNumber`、可选 `totalChunks`、`file` | 幂等上传一个分片，编号从 1 开始 |
| `POST /upload/merge` | `identifier`、`fileName`、`totalChunks` | 校验分片后提交后台合并；视频立即返回可预测的 M3U8 地址 |
| `GET /upload/list` | `page`、`page_size`、`keyword`、`file_type`、`status` | 分页查询上传文件记录 |
| `POST /upload/deleteByPath` | 文件 URL 数组 | 删除上传文件或指定上传会话 |
| `POST /upload/bulk-delete` | `identifiers`：最多 100 个上传 ID | 批量删除上传文件、分片和记录 |
| `DELETE /upload/{identifier}` | 路径中的上传 ID | 删除该 ID 的分类目录、临时分片和上传状态 |
| `GET /uploads/{type}/{前2位}/{第3-4位}/...` | 公开静态地址 | 通过两层分片目录直接访问已完成的上传文件 |

### 分片文件上传

上传视频、图片和其他文件分别保存在 `downloads/uploads/video/`、`downloads/uploads/image/`、`downloads/uploads/other/`，临时分片保存在
`downloads/uploads/chunks/<前2位>/<第3-4位>/<identifier>/`。上传记录和处理状态保存在现有 `tasks.sqlite3` 的单表 `uploads` 中，不使用关联查询；表按更新时间以及“类型、状态、更新时间”建立索引。旧 `upload.json` 和旧目录数据不导入。容器内对应 `/downloads/uploads/`。服务重启后可通过
`GET /upload/chunk?identifier=...` 查询已有分片、后台合并状态并继续上传。重复上传同一分片会原子覆盖。`POST /upload/merge` 只校验分片数量并提交后台任务，不等待文件处理完成；视频响应会立即包含可预测的 HLS、封面、悬浮预览和 VTT 地址。客户端通过 `GET /upload/chunk` 轮询 `status`、`processingStage` 和 `completed`；处理阶段依次为 `merging`、`segmenting`、`generating_cover`、`generating_preview`、`generating_vtt`、`completed`，失败时为 `failed`。后台任务流式写入临时文件并原子发布；合并成功后删除分片，仅保留 SQLite 状态记录用于幂等查询和重复合并。服务重启后会自动继续未完成的处理任务。

分片编号从 1 开始。`identifier` 只允许字母、数字、下划线和连字符；管理页面的分片上传根据文件名、大小和修改时间生成稳定的 16 位十六进制 ID，同一文件重新选择后可继续断点上传。`POST /upload/file` 则在服务端写入文件的同一过程中计算 96 位 BLAKE2b，并编码成 16 位 URL 安全 ID；相同内容直接复用已有资源，不增加完整哈希字段或额外索引。服务端不要求浏览器计算完整文件哈希。脚本、源码、网页、可执行文件及相关配置文件会按文件名后缀拒绝，最终合并后还会检查常见的可执行文件和脚本内容特征；双重后缀也不能绕过。所有上传接口沿用 `JABLE_API_KEY` 鉴权。默认单分片最大 50 MiB、最终文件最大 20 GiB、最多 100000 个分片，可分别通过 `UPLOAD_CHUNK_MAX_BYTES`、`UPLOAD_FILE_MAX_BYTES`、`UPLOAD_MAX_CHUNKS` 调整。接口响应使用 `fileType` 返回 `video`、`image` 或 `other`。所有上传资源按 ID 前四位做两层分片：视频在 `uploads/video/<前2位>/<第3-4位>/<identifier>/` 中生成 `index.m3u8`、四位数字 TS、`cover.jpg`、`poster.jpg`、`preview_image.mp4`、`preview.vtt` 和 `preview*.jpg`；图片和其他单文件分别保存为 `uploads/image/<前2位>/<第3-4位>/<identifier>.<扩展名>` 和 `uploads/other/<前2位>/<第3-4位>/<identifier>.<扩展名>`。悬浮预览默认从视频的 10%、30%、50%、70%、90% 各截取 1.5 秒并拼接为无声 320×180 MP4，短视频自动减少片段。进度预览默认每 10 秒取一帧，200×112 缩略图以 10×10 拼成雪碧图，可通过 `UPLOAD_PREVIEW_INTERVAL` 调整间隔。封面或预览生成失败只写入 `warnings`，不影响 HLS 完成。`UPLOAD_HLS_TIME` 控制分片秒数，默认 3，`UPLOAD_VIDEO_TIMEOUT` 控制单次 FFmpeg 最长处理时间，默认 3600 秒，`UPLOAD_MERGE_WORKERS` 控制后台处理并发数，默认 1。旧的未分片上传目录不迁移、不读取，也不会出现在新接口返回中。

```bash
curl -F "identifier=demo001" -F "chunkNumber=1" -F "totalChunks=2" -F "file=@part1.mp4" http://localhost:8000/upload/chunk
curl -F "identifier=demo001" -F "chunkNumber=2" -F "totalChunks=2" -F "file=@part2.mp4" http://localhost:8000/upload/chunk
curl -H "Content-Type: application/json" -d '{"identifier":"demo001","fileName":"demo.mp4","totalChunks":2}' http://localhost:8000/upload/merge
```

```bash
curl "http://localhost:8000/api/list?page=1"
curl --get "http://localhost:8000/api/detail" --data-urlencode "url=https://jable.tv/videos/example-001/"
```

列表返回格式（示例数据）：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "page": 1,
    "has_next": true,
    "items": [{"id": "example-001", "title": "Example", "url": "https://jable.tv/videos/example-001/", "cover": "https://cdn.example/cover.jpg", "duration": "12:34"}]
  }
}
```

详情的 `data` 包含同样的视频字段，另有 `published_at`、`actors`、`tags`、`m3u8_url`。
`published_at` 优先返回页面“上市於”的日期（如 `2026-09-05`）；缺失时使用页面结构化日期信息。
缺失的普通字段为 `null`，演员和标签缺失时为 `[]`。时间与时长保留来源格式（如 ISO 8601 或 `12:34`）。
播放地址保留完整查询参数，可能过期；过期后重新调用详情接口。API 不代理视频流。

所有 `/api/**` 和 `/upload/**` 接口必须携带 `X-API-Key` 请求头，默认密钥为 `yes@1234`；管理页面和公开媒体目录仍可直接访问。
可在 Compose 同目录的 `.env` 文件配置 `JABLE_API_KEY=你的密钥` 覆盖默认值，并通过 `JABLE_TIMEOUT=30` 设置抓取超时秒数。
本地启动时设置相同环境变量。详情支持 `https://jable.tv/videos/<id>/` 和 `https://123av.com/<语言>/v/<id>`（含对应 www 域名），不接受查询参数、片段及自定义端口。HTTP 重定向逐跳校验，浏览器校验输入和最终地址。

错误统一返回 `{"code": 50202, "message": "未解析到播放地址，页面结构可能变化或访问受限", "data": null}`。

| HTTP 状态 | code | 含义 |
| --- | --- | --- |
| 400 | 40001 / 40002 | 参数错误 / 地址不允许 |
| 401 | 40101 | API Key 无效或缺失 |
| 404 | 40401 | 来源页面不存在 |
| 502 | 50201 / 50202 | 来源访问失败 / 页面解析失败 |
| 502 | 50203 / 50204 | 浏览器未能加载数据 / 浏览器启动或抓取失败 |
| 503 | 50301 | 浏览器抓取繁忙 |
| 504 | 50401 | 抓取超时 |
| 500 | 50000 | 内部错误 |

API 优先使用 HTTP 获取页面；收到 Cloudflare challenge 时回退到项目已有的 Chromium 浏览器。每个进程同时只运行一个抓取浏览器，用完立即关闭。Docker 镜像已包含浏览器；本地运行需安装 Chrome/Chromium 及匹配的 ChromeDriver。浏览器仍无法加载数据时返回明确错误，不将验证页当作空列表成功返回。HTTP 重定向逐跳校验，浏览器回退校验输入地址及最终页面地址。

运行离线接口测试（覆盖分页、解析、鉴权、URL 校验、重定向及异常）：

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

每次看正要爽的時候就給我卡住轉圈圈  

直接下載到電腦看沒煩惱

---

## 🐳 Docker 一鍵啟動（推薦）

不需要手動安裝 ChromeDriver、FFmpeg、Python 環境，全部封裝在容器內。

### 前置需求
- 安裝 [Docker Desktop](https://www.docker.com/products/docker-desktop/)

### 使用方法

```bash
# 1. 建立 API image
docker build -t jable-downloader .

# 2. 启动 API 和管理后台
docker run -d --name jabletvdownload-api --restart unless-stopped -p 8000:8000 -v D:\downloads:/downloads jable-downloader
```

打开 `http://localhost:8000/admin/`，默认 API Key 为 `yes@1234`。镜像已经内置 API 启动命令和默认配置；`PUBLIC_BASE_URL` 与 `DOWNLOAD_DIR` 会显示在 Docker Desktop 的环境变量中，可以直接修改。视频、图片、上传文件、任务数据库和同步密钥都保存在 `DOWNLOAD_DIR`，只需挂载这一个目录。

命令行单视频下载模式使用：

```bash
docker run -it --rm -e URL="https://jable.tv/videos/example/" -v D:\downloads:/downloads jable-downloader python /app/main.py
```

---

## 💻 傳統安裝（Windows）

1. 請自行安裝 ffmpeg，裝完之後執行 INIT.bat 將會自動建置其餘環境。
2. 若收到可以執行 RUN.bat 之訊息，執行 RUN.bat 即可使用此神器。

### 1. 搭建並啟用虛擬環境

```
python -m venv jable
jable/Scripts/activate
```
![image](https://github.com/hcjohn463/JableDownload/blob/main/img/createVenv.PNG)

### 2. 下載所需套件

```
pip install -r requirements.txt
```

安裝 [FFmpeg] 用於轉檔

### 3. 執行程式

```
python main.py
```

### 4. 輸入影片網址
`https://jable.tv/videos/ipx-486/`  
![image](https://github.com/hcjohn463/JableDownload/blob/main/img/download2.PNG)

### 5. 等待下載與合成

下載和合成影片皆有即時進度條顯示：

```
⬇ 下載進度:  73%|██████████████░░░░░  | 1334/1827 [01:12<00:27, 18.2片段/s]
🎬 合成影片:  45%|█████████░░░░░░░░░░░ |  821/1827 [01:23<01:40]
```

### 6. 完成

![image](https://github.com/hcjohn463/JableDownload/blob/main/img/demo2.png)

### 如果覺得好用 再麻煩給個星星好評 謝謝!!

---

[FFmpeg]:<https://www.ffmpeg.org/>

---

## Argument Parser

```bash
python main.py -h
python main.py --random True       # 下載隨機熱門影片
python main.py --url <網址>         # 直接指定 URL
python main.py --all_urls <演員頁>  # 下載演員所有影片
```

---

## ☸️ Kubernetes 支援

`k8s/` 資料夾內含完整 Kubernetes 配置，可將下載任務部署為 K8s Job：

```bash
kubectl apply -f k8s/pvc.yaml        # 建立持久化儲存
kubectl apply -f k8s/configmap.yaml  # 套用設定
kubectl apply -f k8s/job.yaml        # 執行下載任務
kubectl get jobs                      # 查看任務狀態
```

---

## 📜 更新日誌 (Update Log)

| 版本 | 日期 | 內容 |
| :--- | :--- | :--- |
| **v2.0** | 2026/03/15 | 🐳 支援 Docker 容器化部署、☸️ K8s (Job/PVC/ConfigMap) 支援 |
| | | 📊 下載與合成加入 `tqdm` 即時進度條、🚀 優化合成與轉檔速度 |
| **v1.11**| 2023/04/19 | 🦕 新增 ffmpeg 自動轉檔 |
| **v1.10**| 2023/04/19 | 🏹 兼容 Ubuntu Server |
| **v1.9** | 2023/04/15 | 🦅 下載演員所有相關影片 |
| **v1.8** | 2022/01/25 | 🚗 下載結束後自動抓取封面 |
| **v1.7** | 2021/06/04 | 🐶 更改 m3u8 獲取方法 (正則表達式) |
| **v1.6** | 2021/05/28 | 🌏 支援 Unix 系統 (Mac, Linux 等) |
| **v1.5** | 2021/05/27 | 🍎 更新爬蟲網頁方法 |
| **v1.4** | 2021/05/20 | 🌳 修改編碼問題 |
| **v1.3** | 2021/05/06 | 🌈 增加下載進度提示、修改 Crypto 問題 |
| **v1.2** | 2021/05/05 | ⭐ 更新穩定版本 |
