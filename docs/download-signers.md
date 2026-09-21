# 下载签名脚本

管理后台“签名脚本”Tab 按资源 `source` 配置下载时签名。导入、采集和数据库持久化始终保存原始 URL；M3U8、分片、密钥、封面、预览和 VTT 真正发出 HTTP 请求之前，下载传输层才执行对应脚本。签名 URL 与临时请求头只存在内存中，下载计划在 SQLite 中也只保存结构指纹。

每个 `source` 最多启用一个配置。没有配置或配置已停用时，下载器直接使用原始 URL。修改脚本后，后续请求立即使用新版本；服务重启后从原始 URL 重新签名。单次请求失败后的重试会再次执行脚本，因此时间戳和短期 Token 会刷新。

```js
function sign(url, context, helpers, config) {
  const timestamp = helpers.now();
  const value = url + ':' + timestamp;
  const signature = helpers.hmacSha256(config.secret || '', value);

  return {
    url: helpers.withQuery(url, { timestamp, sign: signature }),
    headers: config.referer ? { Referer: config.referer } : {}
  };
}
```

签名函数必须同步返回 URL 字符串，或返回 `{ url, headers }` 对象。不能声明为 `async`。`context` 提供 `task_id`、`source`、`video_id`、`resource_type`、`referer` 和 `original_url`。`resource_type` 可能是 `playlist`、`segment`、`key`、`map`、`cover`、`poster`、`preview_image`、`vtt` 或 `vtt_image`。

`helpers` 提供：

- `now()`：当前 Unix 秒数。
- `md5(value)`、`sha1(value)`、`sha256(value)`。
- `hmacSha1(secret, value)`、`hmacSha256(secret, value)`。
- `base64(value)`、`base64Url(value)`。
- `withQuery(url, object)`：添加或覆盖查询参数。

配置 JSON 通过 `config` 参数传入脚本，并使用独立 AES-GCM 密钥加密保存在数据库中。脚本执行环境使用 QuickJS，不提供文件系统、操作系统、数据库或网络 API。签名结果最多允许 30 个普通请求头，不能覆盖 `Host`、`Content-Length`、`Connection`、`Transfer-Encoding` 或 `Range`。

管理接口全部需要 `X-API-Key`：

- `GET/POST /api/download-signers`
- `PUT/DELETE /api/download-signers/{id}`
- `POST /api/download-signers/test`

测试接口执行尚未保存的脚本和配置，返回临时签名 URL 与请求头，不会发起网络请求或修改下载任务。
