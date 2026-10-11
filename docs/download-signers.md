# 来源管理

管理后台「工具 → 来源管理」按资源 `source` 统一管理图片域名、视频域名、数据格式化脚本和下载签名脚本。来源配置只负责地址解析、数据转换和请求签名，不控制资源是否下载。每个来源保留一条配置，修改后后续导入和请求使用新配置。原有签名脚本保留，新字段默认为空。

## 下载策略

列表采集是否自动下载由任务的 `auto_download_enabled` 配置决定。开启后，该任务通过立即执行、手动执行、批量执行、定时执行或代理采集获得的待下载资源都会加入下载调度；关闭后只保存采集结果。直接 JSON 导入的远程资源默认入队。资源列表中的单条、批量和全部下载始终允许手动入队，实际开始仍受全局调度开关和并发数控制。已经存在于本地的导入资源直接关联为 `completed`。

来源配置没有 `download_enabled` 下载策略。创建或更新来源不会改变已有资源状态，也不会阻止后续手动下载；域名补全、格式化和动态签名仍按来源独立生效。

## 域名与参数

- `image_base_url`：封面、播放器封面、图片类型的悬浮预览、VTT雪碧图的基础地址。
- `video_base_url`：M3U8、TS、初始化片段、密钥、悬浮预览视频和VTT文件的基础地址。
- `config`：供两个脚本共用的JSON配置；可分别保存图片和视频密钥、Referer等参数，沿用AES-GCM加密保存。
- 完整HTTP/HTTPS链接默认保留原域名；相对路径访问时补上对应域名。以 `/` 开头的路径从域名根部解析，其他相对路径从基础地址目录解析。
- 签名脚本或格式化脚本可以留空，分别表示无需签名或直接校验标准资源数组。

## 数据格式化

格式化在服务端QuickJS中执行，后台导入和第三方导入接口使用同一配置。函数必须同步返回1–1000条标准资源，不支持async、网络、文件系统或数据库访问；单次执行限制3秒、32MiB内存。格式化只做字段清洗，保留资源原路径，不生成临时签名。

```js
function format(raw, context, config) {
  const items = Array.isArray(raw) ? raw : (raw.data?.list || raw.items || []);
  return items.map(item => ({
    source: context.source,
    id: String(item.id || item.code),
    title: String(item.title || item.name || '').trim(),
    m3u8_url: item.m3u8_url || item.playUrl,
    cover: item.cover || item.coverUrl || null,
    preview_image: item.preview_image || item.previewUrl || null,
    vtt_url: item.vtt_url || item.vttUrl || null,
    actors: item.actors || [],
    tags: item.tags || [],
    chapters: item.chapters || []
  }));
}
```

`context.source`是当前匹配来源，输出统一使用这个来源。管理页面可测试尚未保存的配置和原始JSON，结果仅用于预览，不创建任务。

## 下载与预览签名

格式化结果入库后，资源URL仍是原始路径。实际发出封面、预览、M3U8、TS、密钥、VTT等请求前才补域名并调用 `sign(url, context, helpers, config)`。签名URL及临时请求头仅保留在内存中，下载重试会重新签名。

```js
function sign(url, context, helpers, config) {
  const key = context.resource_group === 'image' ? config.imageKey : config.videoKey;
  const path = url.replace(/^https?:\/\/[^/]+/i, '').split('?')[0];
  const expires = helpers.now() + 7200;
  return {
    url: helpers.withQuery(url, {
      wsSecret: helpers.md5(key + path + expires),
      wsTime: expires
    }),
    headers: config.referer ? { Referer: config.referer } : {}
  };
}
```

`url`是已补域名的完整链接。`context`提供 `source`、`original_url`、`resource_type`、`resource_group`（image/video）；下载时另有 `task_id`、`video_id`、`referer`。`resource_type`可以是 playlist、segment、key、map、cover、poster、preview_image、vtt或vtt_image。

函数同步返回URL字符串或 `{url, headers}`。`helpers`支持 `now`、`md5`、`sha1`、`sha256`、`hmacSha1`、`hmacSha256`、`base64`、`base64Url`、`withQuery`；不得返回Host、Content-Length、Connection、Transfer-Encoding或Range请求头。

## 列表预览与播放

未下载资源的列表封面、悬浮预览和点击播放通过 `/api/source-media` 转发。管理员请求列表或播放接口后，服务器签发绑定来源、原始资源地址和类型的限时媒体令牌，不暴露API Key或签名密钥。每次远程文件请求都会再次执行来源签名，并改写M3U8中的子清单、TS、密钥、初始化片段和VTT图片地址，使其继续通过同一媒体接口请求。支持GET、HEAD、Range；不把代理URL写回数据库。

下载完成后使用已有本地公开资源；本地媒体接口继续负责R2回源。独立播放器的全局代理开关仍只控制 `/api/player/proxy`，不会关闭管理员授权的资源预览。

## API

管理、导入和播放信息接口需要 `X-API-Key`：

- `GET/POST /api/download-signers`
- `PUT/DELETE /api/download-signers/{id}`
- `POST /api/download-signers/test`：测试域名和签名，增加 `image_base_url`、`video_base_url`、`format_code`，`url`可为相对路径；不访问远程文件。
- `POST /api/download-signers/format-test`：同一配置字段加 `raw`，返回格式化后的 `items`，不导入资源。
- `POST /api/collection-items/import?source=partner_a`：请求体直接为第三方原始JSON；执行该来源格式化规则后校验、入库和排队。
- `POST /api/collection-items/import/validate?source=partner_a`：相同格式化与校验流程，返回 `items`及逐条校验结果，不创建任务。
- 默认无需指定额外参数：数组按每条数据的source匹配配置，原始对象按顶层source匹配配置。导入接口内部执行格式化、校验、入库和排队；未配置或停用的脚本不执行。已格式化的标准数组可以用 `?format=false` 跳过再次格式化。
- `GET /api/collection-items/{id}/playback`：获取本地或授权的远程播放数据及章节。
- `GET/HEAD /api/source-media`：需要服务器签发的媒体令牌；令牌绑定所有请求参数，不能任意改动来源、类型或资源URL。

请求失败、格式化超时或非数组输出返回HTTP400，不加入下载队列；标准数组中的无效条目返回逐条错误，其他有效条目正常导入。
