# JableTVDownload

## 下載JableTV好幫手

## HTTP API

### Vue 3 管理后台

采集资源支持粘贴 JSON 批量导入：可以直接导入标准数组，也可以提交包含source的第三方原始JSON，由导入接口内部自动匹配服务端JavaScript格式化脚本，在服务端完成字段清洗和结果预览；来源域名、格式化和签名统一在来源管理中配置，访问资源时动态签名；播放地址对应本地公开文件时直接关联，其他远程资源通过直接 JSON 导入时默认加入下载队列，通过列表任务采集时由该任务的“采集并自动下载”配置决定是否入队。也可导出选中资源，或把已经下载完成的资源同步到第三方 HTTP 接口。字段示例、脚本格式、去重规则及 API 见 [JSON 资源导入说明](docs/resource-import.md) 和 [资源导出与第三方同步](docs/resource-export-sync.md)。

启动 API 后打开 `http://localhost:8000/admin`。管理页使用 Vue 3、TypeScript、Vite、Element Plus、Pinia、Vue Router、Plyr 和 Hls.js，提供：

- 新建、编辑、删除和手动执行列表采集任务；支持 Jable 最近更新页与 123AV FC2 列表页。任务可选择由当前服务器采集，或交给运行在其他电脑上的采集客户端解析。
- 列表采集任务使用后端分页，默认每页 20 条，可按名称或 URL 模糊搜索，并按来源和任务状态筛选；接口同时支持每日定时开关筛选。自动刷新只读取当前页；翻页和刷新保留多选，主动搜索会清空旧选择。筛选栏可批量删除空闲任务，已采集资源会保留，排队或采集中的任务会跳过。每个任务可独立开启“每天 18:00”定时采集，统一使用北京时间。同一天只调度一次，服务在 18:00 后启动时会补执行当天尚未运行的任务。
- “采集管理”Tab 包含“列表任务”和“采集资源”两个子页。采集资源把采集记录与下载任务合并为一张表，每个视频只显示一条，并集中展示状态、进度、文件大小、错误、播放和删除操作；没有采集记录的直接下载任务也会显示。
- 列表采集只负责保存当前页全部视频信息；有标题时按 NFKC 规范化、合并空白并忽略大小写后去重，无标题时按“来源站点 + 视频标识”去重。采集请求不会等待视频下载。
- 每个列表采集任务可开启“采集并自动下载”。开启后，保存成功的单条任务或本次批量创建的全部新任务立即分批加入列表采集队列；列表数据保存成功后，本次采集到的待下载或失败资源继续加入下载调度，已完成、排队中和下载中的资源自动跳过。手动执行、批量执行和每天18:00定时执行也使用相同的自动下载规则。使用采集客户端时，远程服务器会继续创建详情解析任务，再由客户端返回最新播放地址供服务器下载。任务列表保存上次自动下载的入队、跳过和失败数量。
- 采集资源支持单条“更新详情”和批量“重新采集详情”。该操作重新解析 Jable/123AV 详情页并更新标题、演员、分类标签、普通标签和发布日期等元数据；开启“使用采集客户端”的资源会进入客户端详情更新队列，其余资源由当前服务器解析。操作不删除或重新下载已经完成的视频，也不会修改本地分片、播放地址和下载进度；已完成资源会异步补全缺少的封面和预览附件，正在调度或下载的资源自动跳过。
- 采集资源支持后端分页（20/50/100 条）、标题/番号搜索、资源 URL 或本地公开路径搜索、来源与下载状态筛选，并可手动触发单条下载。标题与番号使用 SQLite FTS5 trigram 索引（关键词至少 3 个字符），URL/路径会先提取资源番号并使用普通索引精确查询；首次升级会自动构建一次全文索引。
- 采集资源支持当前页多选批量下载；“全部下载”会把所有分页中的待下载资源一次性加入下载任务队列并立即返回。入队不会同步下载，实际执行由全局调度开关和并发数控制。
- 选中的资源可导出为统一 JSON 文件，字段包含本地或远程 `m3u8_url`、`vtt_url`、封面、演员和标签。“同步管理 → 同步网站”子页 统一维护多个第三方 HTTP 接口；采集资源的“发送数据”和列表采集任务均可多选这些网站。列表采集命中已完成资源时立即异步发送，新资源在下载完成后自动发送。“发送数据”支持多网站与“强制重新同步”：普通同步跳过已成功和正在同步的资源，强制同步会重新发送已成功资源，但仍跳过正在同步的资源；相同网站和资源始终复用一条同步状态记录。
- 全局自动调度默认开启，下载任务并发数默认1、可设置为1—10；单个任务的TS分片并发默认3、可设置为1—16。密钥和初始化片段先顺序准备，之后并发下载TS；浏览器来源优先为每个线程复用独立HTTP会话，必须回退浏览器时会串行保护WebDriver。关闭调度后已排队任务继续保留，已经运行的任务继续完成；重新开启后继续消费队列，并发降低时不会强制中断运行中的任务。
- 下载策略由列表任务独立配置：`auto_download_enabled` 开启时，本次及后续手动、批量、定时或代理采集到的资源自动加入下载调度；关闭时只保存采集结果。直接 JSON 导入的远程资源默认入队，资源列表中的单条、批量和全部下载始终允许手动入队；实际执行仍受全局调度开关和并发数控制。
- “工具 → 来源管理”子页 按资源 `source` 统一配置图片/视频域名、服务端数据格式化脚本和JavaScript签名函数，不再控制资源是否下载。未下载资源支持授权代理封面、悬浮预览和点击远程播放；下载完成后使用本地资源。数据库只保存原始资源地址；M3U8、TS、密钥、封面与 VTT 请求发出前才动态签名，临时 URL 不落库。配置说明见 [来源管理](docs/download-signers.md)。
- 顶部同时显示“下载网速”和“上传网速”。下载网速由全局共享的 `FileDownloader` 汇总所有下载线程收到的字节；上传网速由 R2 客户端回调汇总全部同步任务及文件并发上传的字节，并分别显示视频组和其他组的实时速度。两者都在内存中按最近 5 秒滑动窗口计算，不依赖数据库进度落库，并在 B/s、KB/s、MB/s、GB/s 之间自动换算；最近 5 秒没有流量时显示 `0 B/s`。
- 下载队列、分片进度、文件大小、错误和警告自动刷新。
- 管理后台只分页请求当前资源页，不再预取 500 条下载任务；顶部数量通过轻量调度状态接口统计，播放优先使用当前资源行中的下载详情。
- 采集资源表格显示 240×135 封面。已加入下载的资源优先使用本地播放器封面或列表封面，尚未保存本地图片时使用采集记录中的远程封面，失败时显示占位图。列表返回的远程封面、悬浮预览和短时预览直接使用来源外链，不经过本地媒体代理，避免来源连接超时占满管理接口连接。鼠标悬浮时播放 `preview_image.mp4/webm` 或来源提供的 `preview_image`，移出后停止并释放播放器；3 分钟和 5 分钟 HLS 预览作为独立字段供播放器或第三方接口使用。
- Jable 列表页采集到的 `preview_image` 会保留到详情下载阶段并保存到视频目录。详情未提供预览或远程预览下载失败时，只要启用了悬浮预览生成，就从本地视频生成 `preview_image.mp4`。
- 下载任务按视频 code（`video_id`，忽略大小写）跨来源防重。后台“按番号去重”可清理历史重复数据：每组保留状态最好、更新时间最新的一条，并删除其他任务及其资源。
- 资源批量删除会拒绝仍在下载的任务；其余资源在 Docker/Linux 中使用系统原生目录删除，并通过一个 SQLite 事务批量清理文件索引和任务记录，避免逐条等待及逐条提交。
- 点击采集资源中有播放地址的资源会弹出Plyr播放器；未下载时使用授权的远程媒体代理，下载完成后使用本地资源；Hls.js 播放本地 `index.m3u8`，并加载本地播放器封面、远程采集封面或内置占位封面，以及 `preview.vtt` 进度缩略图。删除本地文件时保留采集记录并恢复为待下载；没有采集记录的直接下载任务会从资源列表移除。启用 API Key 后，播放器会自动申请 6 小时有效的任务级媒体令牌供缩略图使用。
- “工具 → 播放器”子页 可以输入任意公网 HTTP/HTTPS M3U8 地址播放，并默认尝试同目录的 `poster.jpg`、`cover.jpg` 和 `preview.vtt`，也可手动指定封面、VTT及Referer。全局代理开关保存在数据库中且默认关闭；开关管理需要 `X-API-Key`，开启后公开代理接口无需密钥。代理会改写主/子M3U8、TS、初始化分片、加密Key以及VTT雪碧图地址，支持Range流式转发且不保存远程文件；关闭开关会立即拒绝后续代理请求。代理禁止访问回环、内网、本地域名和云元数据地址。
- 123AV 多章节资源仍保存为一条任务。所有章节按 `01/`、`02/` 等目录依次下载，每章的 TS、`index.m3u8`、`preview.vtt`、`preview.m3u8` 和 `preview5.m3u8` 放在同一章节目录；总进度聚合全部章节，重启后跳过已经完成的分片。任务接口、JSON 导出和节点同步会在 `chapters` 中返回每章自己的 3 分钟及 5 分钟预览地址；根级预览地址、列表封面、悬浮预览和默认播放仍使用第一章节。
- 管理页打开时必须输入 `X-API-Key` 并通过服务端验证；密钥只保存在当前浏览器的 `localStorage`。右上角钥匙按钮可重新验证密钥。
- 列表任务新增API接口采集：支持GET/POST、第三方鉴权请求头、JSON参数、默认source、`${page}`分页模板和每天18点定时执行；批量POST可在请求JSON中使用 `${page}`，接口地址本身无需包含变量。也可由代理客户端拉取原始JSON。服务器统一按来源格式化并导入，下载由每个列表任务的 `auto_download_enabled` 控制，单条、批量、定时和代理采集使用相同规则，并展示获取/新增/更新/跳过/失败/入队数量。配置与管理接口见 [API接口列表采集](docs/api-list-collection.md)。
- 管理页提供独立的“上传文件”Tab，使用后端分页并固定按创建时间倒序排列，状态和同步进度更新不会改变已有记录的位置；可按文件名、资源 ID 或任一本地资源 URL 搜索，并支持类型与状态筛选、预览、单条删除和多选批量删除。R2同步完成的资源可单条或批量删除本地文件，同时保留上传记录和R2对象；原 `/uploads/...` 预览地址保持不变，本地文件不存在时由服务端从R2回源。资源 URL 会直接解析两层目录中的资源 ID，并走主键查询，不扫描全部路径字段。列表展示图片和视频分辨率，并根据 `height > width` 只显示“竖屏”或“横屏”；字段默认横屏，历史数据不回填。上传对话框把文件按 10 MiB 分片顺序上传，并在重试时查询和跳过已有分片；完成后可直接预览图片、音频、PDF 和视频，其他文件可下载。“工具 → 分片上传文档”子页 提供鉴权说明、接口流程以及可复制的浏览器原生 JavaScript 完整案例。上传视频由服务端 FFmpeg 按需压缩或直接流复制，生成 3 秒 VOD HLS，并自动生成列表封面 `cover.jpg`、播放器封面 `poster.jpg`、多时段拼接的悬浮预览 `preview_image.mp4`、进度预览 `preview.vtt` 及雪碧图。全部处理结束后删除合并产生的原视频。
- “工具 → FFmpeg任务”子页 统一展示上传视频的排队、媒体探测、按需压缩、HLS 切片、封面和预览生成进度。一个视频的完整处理流程占用一个并发名额；并发数默认 1，设置为 0 时暂停领取新任务，已经运行的任务继续完成。并发设置和任务记录保存在 SQLite，服务重启会把未完成任务重新排队。页面支持文件名/资源 ID 搜索、状态筛选、多选重试、批量取消排队任务以及删除终态任务记录；删除任务记录不会删除上传资源。并发和压缩参数统一在顶部“系统配置”中修改。
- “同步管理 → R2同步日志”子页 统一展示采集资源、上传资源的后台同步进度。同步调度分为彼此独立的“视频组”和“其他组”：采集视频与上传视频进入视频组，图片和其他上传文件进入其他组；两组各自配置任务并发和单资源文件并发，其中任一任务并发设为0只暂停对应组。视频目录中的TS、封面、预览图等并发上传完成后才发布VTT和M3U8，整个目录成功后才把资源标记为同步完成并执行本地清理，因此慢视频不会阻塞小图片和普通文件。R2对象键统一按“R2路径前缀 + 本地公开URL路径”生成；修改路径前缀后再次同步旧资源时会检测目标对象键变化并重新上传，不会复用旧前缀的完成记录。上传文件的图片、视频和其他文件始终通过原 `/uploads/video`、`/uploads/image`、`/uploads/other` 地址预览：本地文件存在时直接返回，本地缺失时使用当前R2配置和URL路径直接回源，支持视频 Range 请求，不查询资源表或R2文件明细表，也不要求浏览器跨域访问R2。采集资源仍按其R2公开地址展示。R2连接、公开地址、路径前缀、四项并发、自动同步和本地保留策略统一在顶部“系统配置”中修改。开启全局自动同步后，后台分批补齐全部已完成资源，未来完成的资源自动入队。网络超时、断连、限流及R2的5xx错误在SDK请求重试仍失败后，会按1、5、15、30分钟自动重新排队；重试次数和下次执行时间持久化到数据库，服务重启后继续等待。配置错误、鉴权失败和本地文件缺失直接进入失败，避免无限循环；手动重试会重新开始自动重试次数。采集资源和上传文件直接保存 `r2_sync_status`，状态分为“未同步、同步中、同步完成”；加入队列和失败重试立即进入同步中，成功后进入同步完成，失败回到未同步。本地保留天数小于等于0时统一保存为0，并在R2确认同步成功后立即删除本地资源；设置1—3650天时从实际同步完成时间起保留，后台每5分钟扫描到期资源。清理保留数据库、R2对象和同步日志，并记录本地清理状态。任务记录文件数、字节数、公开地址、同步组和错误，支持分页搜索、同步组/来源/状态筛选、失败重试及批量删除日志。服务重启会继续未完成任务。Secret Access Key加密保存在现有数据目录；删除日志或本地资源不会删除已经写入R2的对象。
- 顶部实时显示容器CPU和内存使用率，顶部“运行监控”展开后展示容器CPU、内存、下载磁盘、API进程和FFmpeg进程信息，并提供5分钟、15分钟、1小时、6小时、24小时和3天趋势图。默认每5秒采样到现有SQLite，超过1小时的查询自动按30秒、2分钟或5分钟聚合，系统每小时清理3天以前的数据。Docker环境优先读取cgroup的CPU配额、内存使用量和内存上限；未限制内存时使用容器可见的系统内存。FFmpeg进程表显示PID、关联任务、阶段、进度、CPU、内存、处理速度、帧数和媒体时间。`SYSTEM_METRICS_INTERVAL` 可调整采样秒数，允许2—60秒。

采集任务、采集资源、调度设置和下载任务共同持久化在 `downloads/tasks.sqlite3`。重启服务后全部数据和并发设置仍然存在；中断的采集执行会回到空闲状态，之后可手动再次执行。当前版本每次只采集传入链接对应的当前页，不自动翻页。关闭 `auto_download_enabled` 的列表任务只保存采集结果；开启后会把本次采集到的待下载资源自动加入队列。用户也可随时通过单条、批量或“全部下载”手动入队。

管理后台顶部统一为“采集管理、上传文件、代理采集、同步管理、工具”五个Tab，默认进入“采集管理 → 采集资源”。采集管理包含列表任务和采集资源，记住当前浏览器上次打开的子页；两张表各自保留搜索、分页和多选状态。代理采集包含远程任务调度和Token管理；同步管理包含同步网站、发送记录和R2同步日志；工具依次包含FFmpeg任务、来源管理、分片上传文档和播放器。各组记住当前浏览器上次选择的子页，子页首次打开时加载，任务列表离开页面后停止轮询。发送记录统一展示同步网站和历史下载节点的结果，支持分页搜索、失败重试和删除记录。网速与运行监控继续放在顶部，系统配置由顶部按钮打开。

远程服务器无法访问来源页面时，可在采集任务中开启“使用采集客户端”。先在远程服务器的“代理采集 → Token管理”子页创建节点，配置名称和启用状态；创建或重置时生成22位随机Token，管理员可随时在Token管理表格中查看和复制完整Token。服务器保存完整Token供管理页面展示，并继续使用SHA-256哈希索引认证；普通节点状态查询和采集节点鉴权响应不返回Token。以前仅保存哈希的节点，会在客户端下一次携带有效Token连接时补存完整Token，也可以手动重置。个人电脑在“代理采集 → 远程任务调度”子页填写远程地址和节点Token，并设置本地采集并发及自动启动。同一远程服务器只能保存一条本地客户端配置。Token在个人电脑使用AES-GCM加密保存。节点默认领取所有列表和详情任务，不设置来源权限或服务端并发限制；采集请求通过 `X-Collector-Token` 鉴权，无需远程管理密钥。停用节点后停止领取新任务，已领取任务仍可续期和提交；删除或重置Token会立即使旧Token失效，并将租约中的任务重新排队。远程服务器持有唯一的 `tasks.sqlite3`，节点配置不会因为离线而删除；旧的自动注册节点不再展示，旧客户端配置需要编辑并填入节点Token。客户端每30秒续期，120秒未续期的租约重新开放领取。列表数据和详情写回远程服务器后，由远程服务器继续执行下载、FFmpeg、R2和删除。

下载地址采用执行时解析：列表采集、单条下载、批量下载和全部下载只把资源加入队列，不提前创建一整批播放地址解析任务。下载线程取得并发名额后，服务器采集资源重新解析详情页；代理采集资源才创建优先的详情任务，并保留这个下载名额，客户端返回结果后当前线程立即下载，不重新排队。第三方导入资源继续在每个文件请求前动态签名。解析中的资源计入下载并发，因此下载并发3最多同时准备或下载3个资源。

同一服务器可连接多个持有独立Token的客户端，采集并发由每台客户端配置。任务用SQLite事务原子领取、绑定节点和租约令牌；详情提交再次在同一事务中验证租约，拒绝其他节点、旧租约或已释放名额的迟到结果。下载前的详情任务优先于普通列表和手动详情刷新。没有在线节点时不占用下载名额，其他可下载资源仍能执行；解析超过180秒时取消本次任务、释放名额并持久化30秒后的重试时间。重启将中断的准备任务重新排队，并使旧的下载地址解析租约失效，保留已有视频分片。

下载中遇到401/403/410或无效M3U8时，停止重试旧地址，最多再获取最新地址两次；代理来源重新请求客户端，服务器来源重新解析详情，第三方来源重新动态签名。已校验完成的分片继续复用。分片清单结构或密钥变化时立即报错并保留原文件，禁止混合不同版本。

“采集客户端”运行在打开管理页所连接的后端主机上。需要使用家里网络时，应在家里电脑运行一套本项目并从其管理页启动客户端；仅在浏览器中打开远程服务器管理页不会使用家里网络。`collector_agent.py` 命令行方式继续保留，适合无管理页面的环境：

```powershell
python collector_agent.py `
  --server http://43.225.196.162:9178 `
  --token "远程节点生成的22位Token" `
  --concurrency 1
```

也可设置 `COLLECTOR_SERVER_URL`、`COLLECTOR_TOKEN` 和 `COLLECTOR_CONCURRENCY` 环境变量。采集节点管理接口需要全局 `X-API-Key`，客户端仅使用专用节点Token，无法调用管理、删除资源或系统配置接口。

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

资源下载完成后，任务表中的 `cover_url`、`preview_url`、`vtt_url`、`m3u8_url`、`preview_m3u8_url` 和 `preview5_m3u8_url` 会替换为公开静态地址；远程来源继续保存在 `metadata_json` 中供恢复下载使用。`PUBLIC_BASE_URL` 控制公开地址前缀，未设置时默认 `http://localhost:8000`。管理后台顶部“系统配置”中的公开访问地址可以动态覆盖环境变量，保存后立即生效并持久化到现有 SQLite；优先级为后台配置、环境变量、系统默认。接口响应、上传文件、JSON 导出和节点同步会根据本地相对文件字段使用当前地址，不移动文件，也不批量迁移旧记录。例如 `downloads/jable/abc/cover.jpg` 对应 `http://localhost:8000/jable/abc/cover.jpg`，`downloads/123/fc2-ppv-4975150/index.m3u8` 对应 `http://localhost:8000/123/fc2-ppv-4975150/index.m3u8`。Jable、123AV 和第三方导入的视频下载完成后，每个章节都会生成直接复用该章现有分片、最长 180 秒的 `preview.m3u8` 以及最长 300 秒的 `preview5.m3u8`；第三方导入资源还会用第一章本地 `index.m3u8` 补齐缺失的封面、悬浮预览和 VTT。这些附加资源失败只写入对应章节警告，不改变视频的完成状态。管理页和第三方系统可以直接使用这些完整地址。
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
| `GET/POST /api/collections` | GET：`page`、`page_size`、`keyword`、`source`、`status`、`scheduled_enabled`；POST：名称、列表链接、可选 `r2_sync_enabled`、`local_collect_enabled`、`auto_download_enabled` | 分页查询或创建采集任务；开启 `local_collect_enabled` 后由采集客户端解析，开启 `auto_download_enabled` 后采集成功立即加入下载调度 |
| `POST /api/collections/bulk-create` | `name`、`url_template`、`start_page`、`end_page`、`scheduled_enabled`、可选 `request_body`、`r2_sync_enabled`、`local_collect_enabled`、`auto_download_enabled` | 用 URL 或 API POST JSON 中的 `${page}` 批量新建最多 500 个任务，所有任务继承采集位置和自动下载配置 |
| `PUT/DELETE /api/collections/{id}` | 采集任务 ID | 修改或删除空闲采集任务 |
| `POST /api/collections/{id}/execute` | 采集任务 ID | 异步触发一次列表采集 |
| `POST /api/collections/bulk-execute` | `ids`：最多 100 个采集任务 ID | 批量触发列表采集 |
| `POST /api/collections/bulk-delete` | `ids`：最多 100 个采集任务 ID | 批量删除空闲采集任务并保留已采集资源 |
| `GET /api/collector/nodes` | 无 | 查看采集客户端在线状态和等待、执行中、失败任务数量 |
| `GET/POST /api/collector-nodes` | POST：`name`、`enabled` | 节点管理；创建时返回一次22位Token，使用全局 `X-API-Key` |
| `PUT/DELETE /api/collector-nodes/{id}` | PUT：`name`、`enabled` | 修改名称、启停或删除节点 |
| `POST /api/collector-nodes/{id}/rotate-token` | 无 | 重置Token，旧Token立即失效并重新排队未完成任务 |
| `GET /api/collector/check` | `X-Collector-Token` 请求头 | 测试节点Token，返回节点名称和启用状态并更新心跳 |
| `GET/POST /api/local-collectors` | POST：名称、远程地址、22位 `token`、并发数、自动启动 | 查询或新增由当前后端进程运行的采集客户端配置 |
| `PUT/DELETE /api/local-collectors/{id}` | 客户端配置 ID | 修改或删除已经停止的采集客户端配置 |
| `POST /api/local-collectors/{id}/test` | 客户端配置 ID | 验证远程服务鉴权并返回连接延迟 |
| `POST /api/local-collectors/{id}/start` | 客户端配置 ID | 启动该客户端的采集工作线程 |
| `POST /api/local-collectors/{id}/stop` | 客户端配置 ID | 停止领取新任务，当前请求结束后完全停止 |
| `POST /api/local-collectors/bulk-start` | `ids`：客户端配置 ID 数组 | 批量启动客户端 |
| `POST /api/local-collectors/bulk-stop` | `ids`：客户端配置 ID 数组 | 批量停止客户端 |
| `POST /api/collector/jobs/claim` | 无请求体；`X-Collector-Token` 请求头 | 领取一个120秒租约任务；停用节点返回空任务 |
| `POST /api/collector/jobs/{id}/heartbeat` | `lease_token`；`X-Collector-Token` 请求头 | 续期本节点正在执行的采集任务 |
| `POST /api/collector/jobs/{id}/complete` | `lease_token`、`result`；`X-Collector-Token` 请求头 | 写回本节点列表或详情结果，并继续远程下载调度 |
| `POST /api/collector/jobs/{id}/fail` | `lease_token`、`error`；`X-Collector-Token` 请求头 | 记录本节点采集失败，允许重新执行 |
| `GET /api/collection-items` | `page`、`page_size`、`title`、`source`、`status` | 分页查询统一资源列表，返回采集信息和关联下载详情，并包含未关联的直接下载任务 |
| `POST /api/collection-items/import` | 1–1000 条资源 JSON 数组 | 本地播放文件直接关联为已完成，远程资源自动加入下载队列 |
| `POST /api/collection-items/import/validate` | 1–1000 条资源 JSON 数组 | 使用正式导入规则预览校验、去重及本地资源判断，不写入数据 |
| `GET/POST /api/import-scripts` | POST：名称、说明、JavaScript 代码 | 兼容旧格式化脚本存储；新功能统一在来源管理配置 |
| `PUT/DELETE /api/import-scripts/{id}` | 脚本 ID | 修改或删除导入格式化脚本 |
| `GET/POST /api/download-signers` | POST：名称、source、图片/视频域名、格式化脚本、签名脚本、配置 | 统一查询或新增来源配置 |
| `PUT/DELETE /api/download-signers/{id}` | 来源配置 ID | 修改或删除统一来源配置 |
| `POST /api/download-signers/format-test` | 来源配置字段、raw | 服务端格式化预览，不创建任务 |
| `GET /api/collection-items/{id}/playback` | 资源 ID | 返回本地或授权的远程播放数据及章节 |
| `GET/HEAD /api/source-media` | source、url、resource_type、媒体令牌 | 逐次签名并代理远程媒体，支持HLS、VTT和Range |
| `POST /api/download-signers/test` | 脚本、配置、测试 URL | 测试动态签名结果，不发送资源请求 |
| `POST /api/collection-items/{id}/download` | 采集资源 ID | 手动加入下载调度 |
| `POST /api/collection-items/bulk-download` | `ids`：最多 100 个采集资源 ID | 批量加入下载调度 |
| `POST /api/collection-items/bulk-delete` | `ids`：最多 100 个资源 ID | 批量停止任务并删除本地文件和数据库记录 |
| `POST /api/collection-items/download-all` | 无 | 将所有待下载采集资源批量加入下载任务队列 |
| `GET/PUT /api/settings/scheduler` | `enabled`、`concurrency`、`segment_concurrency` | 查询或更新全局调度开关、下载任务并发和单任务TS分片并发（默认3） |
| `GET/PUT /api/settings/public-url` | `url` | 查询或动态保存公开访问根地址，保存后立即生效 |
| `GET/PUT /api/player-proxy/settings` | PUT：`enabled` | 查询或修改全局播放器代理开关；需要 `X-API-Key` |
| `GET/HEAD /api/player/proxy` | `url`，可选 `referer` | 开关开启时公开代理M3U8、TS、VTT、Key、图片等远程资源；无需 `X-API-Key` |
| `GET /api/resource-sync/records` | `page`、`page_size`、`keyword`、`status`、`site_id` | 兼容旧版下载节点发送明细，管理页不再展示 |
| `POST /api/resource-sync/records/retry` | `ids` 或 `all=true` | 多选重试或重试全部失败发送记录 |
| `POST /api/resource-sync/records/delete` | `ids` | 删除当前发送状态，不删除本地或节点资源；删除后允许重新调度 |
| `GET/PUT /api/upload/chunk-cleanup/settings` | GET 查询；PUT 传 `enabled`、`retentionDays` | 查询或保存未完成上传分片清理配置 |
| `POST /api/upload/chunk-cleanup/run` | 无 | 立即清理已经过期的新分片上传任务 |
| `POST /upload/file` | `multipart/form-data` 的 `file` | 直接上传完整文件；按 96 位内容哈希去重并返回 `deduplicated` |
| `GET /upload/chunk` | `identifier` | 查询已上传分片和合并状态 |
| `POST /upload/chunk` | `identifier`、`chunkNumber`、`totalChunks`、`file`，可选 `r2SyncEnabled` | 幂等上传一个分片，编号从 1 开始；无需携带原始文件名 |
| `POST /upload/merge` | `identifier`、`fileName`，可选 `r2SyncEnabled` | 从上传记录读取分片总数，根据文件名识别类型，校验分片后提交后台合并；视频立即返回可预测的 M3U8 地址 |
| `GET /upload/list` | `page`、`page_size`、`keyword`、`file_type`、`status` | 分页查询上传文件记录 |
| `POST /upload/deleteByPath` | 文件 URL 数组 | 删除上传文件或指定上传会话 |
| `POST /upload/bulk-delete` | `identifiers`：最多 100 个上传 ID | 批量删除上传文件、分片和记录 |
| `DELETE /upload/{identifier}` | 路径中的上传 ID | 删除该 ID 的分类目录、临时分片和上传状态 |
| `GET /uploads/{type}/{前2位}/{第3-4位}/...` | 公开预览地址 | 本地文件存在时直接返回，本地缺失时根据当前R2前缀透明回源 |
| `GET/PUT /api/r2/settings` | R2 Endpoint、Access Key、Bucket、公开地址、前缀、任务并发、文件并发和两个自动同步开关 | 查询或保存CF R2配置；单资源文件并发默认3 |
| `POST /api/r2/settings/test` | 同上 | 使用当前表单参数测试R2连接 |
| `GET /api/r2/tasks` | `page`、`page_size`、`keyword`、`status`、`resource_type`、`sync_group` | 分页查询R2资源同步日志；同步组可选 `video`、`other` |
| `POST /api/r2/tasks/retry` | `ids`：最多100个日志ID | 重新调度失败的R2同步任务 |
| `POST /api/r2/tasks/delete` | `ids`：最多100个日志ID | 删除非运行中的同步日志，保留R2对象和本地资源 |
| `POST /api/r2/resources/sync` | `ids`：最多100个已完成下载任务ID | 将已完成的采集/下载资源加入R2同步队列 |
| `POST /api/r2/uploads/sync` | `ids`：最多100个上传资源ID | 将已完成的上传资源加入R2同步队列 |
| `POST /api/r2/uploads/cleanup-local` | `ids`：最多100个上传资源ID | 删除已完成R2同步的本地文件，保留上传记录和R2对象 |

### 分片文件上传

上传视频、图片和其他文件分别保存在 `downloads/uploads/video/`、`downloads/uploads/image/`、`downloads/uploads/other/`，临时分片保存在
`downloads/uploads/chunks/<前2位>/<第3-4位>/<identifier>/`。上传记录和处理状态保存在现有 `tasks.sqlite3` 的单表 `uploads` 中，不使用关联查询；表按更新时间以及“类型、状态、更新时间”建立索引。旧 `upload.json` 和旧目录数据不导入。容器内对应 `/downloads/uploads/`。服务重启后可通过
`GET /upload/chunk?identifier=...` 查询已有分片、后台合并状态并继续上传。重复上传同一分片会原子覆盖。上传分片时只记录 `totalChunks`，不依赖 multipart 中的原始文件名；`POST /upload/merge` 提交 `identifier` 和 `fileName`，由文件名决定资源类型与扩展名。合并接口校验分片数量并提交后台任务，不等待文件处理完成；视频响应会立即包含可预测的 HLS、封面、悬浮预览和 VTT 地址。客户端通过 `GET /upload/chunk` 轮询 `status`、`processingStage` 和 `completed`；处理阶段依次为 `merging`、`probing`、按需进入 `compressing`（否则为 `segmenting`）、`generating_cover`、`generating_preview`、`generating_vtt`、`completed`，失败时为 `failed`。后台任务流式写入临时文件并原子发布；合并成功后删除分片，仅保留 SQLite 状态记录用于幂等查询和重复合并。服务重启后会自动继续未完成的处理任务。

分片编号从 1 开始。`identifier` 只允许字母、数字、下划线和连字符；管理页面在 HTTPS 或 localhost 环境下读取文件大小及首尾各 16 KiB，使用浏览器原生 SHA-256 生成 16 位十六进制 ID，小于等于 32 KiB 的文件读取全部内容。普通 HTTP 环境无法使用 `crypto.subtle` 时，自动根据文件名、大小和修改时间使用 FNV-1a 64 位算法生成同长度 ID。两种方式都不会读取整个大文件，同一文件重新选择后可继续断点上传。管理后台“工具 → 分片上传文档”子页 提供携带 `X-API-Key` 的完整 Vue 3 直连客户端和浏览器原生 JavaScript 直连示例，也提供由服务器保存密钥的 Java Spring Boot、PHP cURL 转发示例；其中“Java现有接口兼容”示例可保留 `/upload/image`、`/upload/video` 以及 `appId/requestId/size/index/file` 参数，并在服务端转换成本项目协议。`POST /upload/file` 则在服务端写入文件的同一过程中计算 96 位 BLAKE2b，并编码成 16 位 URL 安全 ID；相同内容直接复用已有资源，不增加完整哈希字段或额外索引。服务端不要求浏览器计算完整文件哈希。脚本、源码、网页、可执行文件及相关配置文件会按文件名后缀拒绝，最终合并后还会检查常见的可执行文件和脚本内容特征；双重后缀也不能绕过。所有上传接口沿用 `JABLE_API_KEY` 鉴权。默认单分片最大 50 MiB、最终文件最大 20 GiB、最多 100000 个分片，可分别通过 `UPLOAD_CHUNK_MAX_BYTES`、`UPLOAD_FILE_MAX_BYTES`、`UPLOAD_MAX_CHUNKS` 调整。接口响应使用 `fileType` 返回 `video`、`image` 或 `other`。所有上传资源按 ID 前四位做两层分片：视频在 `uploads/video/<前2位>/<第3-4位>/<identifier>/` 中生成 `index.m3u8`、四位数字 TS、`cover.jpg`、`poster.jpg`、`preview_image.mp4`、`preview.vtt`、`preview*.jpg`、`preview.m3u8` 和 `preview5.m3u8`；图片和其他单文件分别保存为 `uploads/image/<前2位>/<第3-4位>/<identifier>.<扩展名>` 和 `uploads/other/<前2位>/<第3-4位>/<identifier>.<扩展名>`。悬浮预览默认从视频的 10%、30%、50%、70%、90% 各截取约 3 秒并拼接为无声 320×180 MP4，短视频自动减少片段或缩短单段时长。进度预览默认每 10 秒取一帧，200×112 缩略图以 10×10 拼成雪碧图，可通过 `UPLOAD_PREVIEW_INTERVAL` 调整间隔。上传视频还会复用本地 TS 分片生成最长 3 分钟和 5 分钟的短时 HLS 清单。封面或预览生成失败只写入 `warnings`，不影响 HLS 完成。`UPLOAD_HLS_TIME` 控制分片秒数，默认 3，`UPLOAD_VIDEO_TIMEOUT` 控制单次 FFmpeg 最长处理时间，默认 3600 秒，`UPLOAD_MERGE_WORKERS` 控制后台处理并发数，默认 1。旧的未分片上传目录不迁移、不读取，也不会出现在新接口返回中。

新建或继续上传的分片任务会在独立的 `upload_chunk_cleanup_queue` 延迟队列表中保存一条到期记录，每收到一个分片只更新该记录的到期时间、分片数和占用空间。合并、删除或清理完成时同步移除队列记录；升级前已有任务不会补入队列。后台清理线程通过到期索引只读取已经到期的记录，并直接等待最近一次到期时间；新分片和配置变更会立即唤醒线程重新计算，无需定时扫描全部上传数据。默认清理超过 7 天没有活动且仍为 `uploading` 的任务，同时删除整个分片目录和数据库记录；合并中、FFmpeg处理中及已完成任务不会清理。可在“系统配置 → 上传分片”调整开关和保留天数、查看受管理任务数量与占用空间，并立即清理已过期任务。首次启动默认值可通过 `UPLOAD_CHUNK_CLEANUP_ENABLED` 和 `UPLOAD_CHUNK_RETENTION_DAYS` 设置。

上传图片和视频都会用 FFprobe 读取显示宽高；图片额外保存 `imageFormat`，视频继续读取时长、编码和码率。宽高会考虑媒体旋转信息，并保存 `isPortrait`，只有 `height > width` 时为 `true`，其他情况默认为 `false`。视频不是 H.264、音频不是 AAC，或者宽高超过后台配置的输出清晰度时会转码。后台使用 `360P`、`480P`、`720P`、`1080P`、`2K / 1440P`、`4K / 2160P` 枚举，默认1080P；限制最长边并保持原始比例，因此横屏1080P约为1920×1080，竖屏自动输出约1080×1920。开启“忽略大小与码率规则”后，兼容编码且没有超过分辨率的视频直接复制为 HLS 切片；关闭后，大小、时长和码率阈值也参与压缩判断。转码使用 H.264、AAC 128k、CRF 25 和 `superfast`，不放大小分辨率视频。顶部“系统配置”的 FFmpeg 页面可动态设置并发、兼容性压缩策略、大小/时长阈值、最大码率、输出清晰度、CRF、preset、音频码率、HLS分片时长，并独立控制列表悬浮预览视频与播放器VTT进度预览的生成。关闭预览开关只影响之后处理的资源，不删除现有文件，也不影响来源站点已经提供的预览资源。配置保存在SQLite；保存后影响等待中和之后新增的任务，运行中的任务使用启动时取得的配置快照。首次启动默认值可通过 `UPLOAD_COMPRESS_ENABLED`、`UPLOAD_COMPATIBILITY_ONLY_COMPRESSION`、`UPLOAD_GENERATE_HOVER_PREVIEW`、`UPLOAD_GENERATE_VTT_PREVIEW`、`UPLOAD_COMPRESS_MIN_BYTES`、`UPLOAD_COMPRESS_MIN_DURATION`、`UPLOAD_COMPRESS_MAX_WIDTH`、`UPLOAD_COMPRESS_MAX_HEIGHT`、`UPLOAD_COMPRESS_MAX_VIDEO_BITRATE`、`UPLOAD_COMPRESS_CRF`、`UPLOAD_COMPRESS_PRESET`、`UPLOAD_AUDIO_BITRATE` 和 `UPLOAD_HLS_TIME` 覆盖。完成响应和上传列表会返回 `duration`、`durationText`、`width`、`height`、`isPortrait`、`imageFormat`、`videoCodec`、`audioCodec`、`bitRate`、`originalSize`、`processedSize`、`compressed` 与 `compressionRatio`；`compressionRatio` 表示最终 TS 总大小相对原文件减少的百分比。

新上传的 JPEG、PNG 和 WebP 图片默认在大于 5 MiB 时尝试压缩。JPEG/WebP 默认质量为 82，PNG 使用无损压缩，GIF 动图保持原文件；压缩不会改变图片分辨率或宽高比，并且只有结果更小时才会原子替换原图。失败或没有节省空间时仍保留原图并在上传记录中返回提示。可在顶部“系统配置 → 图片压缩”动态调整开关、大小阈值和质量，也可用 `UPLOAD_IMAGE_COMPRESS_ENABLED`、`UPLOAD_IMAGE_COMPRESS_MIN_BYTES`、`UPLOAD_IMAGE_COMPRESS_QUALITY` 设置首次启动默认值。图片处理完成后会保存最终的 `width`、`height`、`imageFormat`、`isPortrait`、`processedSize`、`compressed` 和 `compressionRatio`，R2 自动同步使用处理后的最终文件。

视频文件合并后会写入持久化 FFmpeg 队列，`POST /upload/merge` 和 `POST /upload/file` 不等待视频转码完成。`UPLOAD_FFMPEG_CONCURRENCY` 只用于数据库尚无设置时的初始并发数，默认 1；之后以管理页面保存的数据库设置为准。`UPLOAD_FFMPEG_MAX_CONCURRENCY` 控制可设置的上限，默认 16。并发数为 0 时暂停调度；降低并发不会杀死正在运行的 FFmpeg。处理失败保留合并后的原视频供重试，全部资源成功发布后才删除原视频。

下载任务只在等待、下载和失败状态保留逐个 TS 分片的 `files` 数据库记录，用于校验文件和断点续传。任务完整下载并生成预览后，会在同一个数据库事务中把总分片数、已下载数和字节数保存到 `tasks`，把各章节的最终进度保存到 `metadata_json.chapters`，随后删除该任务 `kind='segment'` 的记录；磁盘上的 TS、M3U8、封面和预览文件不会删除，密钥及附加资源记录也会保留。服务升级后首次启动会用后台线程分批压缩历史已完成任务，不阻塞 API 启动；未完成或数据不完整的任务不会被清理。SQLite 删除的页会自动供后续写入复用，但不会主动执行会锁库的 `VACUUM`。

```bash
curl -F "identifier=demo001" -F "chunkNumber=1" -F "totalChunks=2" -F "file=@part1.mp4" http://localhost:8000/upload/chunk
curl -F "identifier=demo001" -F "chunkNumber=2" -F "totalChunks=2" -F "file=@part2.mp4" http://localhost:8000/upload/chunk
curl -H "Content-Type: application/json" -d '{"identifier":"demo001","fileName":"demo.mp4"}' http://localhost:8000/upload/merge
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

详情的 `data` 包含同样的视频字段，另有 `published_at`、`actors`、`tags`、`m3u8_url` 和 `chapters`。Jable 根据页面演员链接数量优先从中文视频标题末尾提取本地化演员名；标题无法提取时，才回退到头像或占位元素的 `data-original-title` 以及链接文字。多章节资源的 `m3u8_url`、`vtt_url` 保持指向第一章节，`chapters` 返回每章的序号、名称、时长、播放地址和 VTT 地址。
`published_at` 优先返回页面“上市於”的日期（如 `2026-09-05`）；缺失时使用页面结构化日期信息。
缺失的普通字段为 `null`，演员和标签缺失时为 `[]`。时间与时长保留来源格式（如 ISO 8601 或 `12:34`）。
采集资源表格通过独立的“作者 / 标签”列展示演员、分类、普通标签和发布日期；旧资源可使用“更新详情”或“重新采集详情”补齐，已下载视频不会因此重新下载。
播放地址保留完整查询参数，可能过期；过期后重新调用详情接口。API 不代理视频流。

管理和上传接口必须携带 `X-API-Key` 请求头，默认密钥为 `yes@1234`。采集客户端的 `/api/collector/check` 和 `/api/collector/jobs/**` 使用专用 `X-Collector-Token`；开启全局播放器代理时 `/api/player/proxy` 无需密钥。管理页面和公开媒体目录可直接访问。
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

API 优先使用 HTTP 获取页面；收到 Cloudflare challenge 时回退到项目已有的 Chromium 浏览器。每个进程同时只操作一个抓取浏览器，但验证成功后会持续复用该浏览器、Cookie和User-Agent，批量采集多页时不再为每页反复启动Chromium；浏览器进程失效或页面等待超时会自动销毁，下次请求重新创建，服务停止时统一释放。Docker 镜像已包含浏览器；本地运行需安装 Chrome/Chromium 及匹配的 ChromeDriver。浏览器仍无法加载数据时返回明确错误，不将验证页当作空列表成功返回。HTTP 重定向逐跳校验，浏览器回退校验输入地址及最终页面地址。

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
docker run -d --name jabletvdownload-api --restart unless-stopped --cpus=8 -p 8000:8000 -v D:\downloads:/downloads jable-downloader
```

打开 `http://localhost:8000/admin/`，默认 API Key 为 `yes@1234`。镜像已经内置 API 启动命令和默认配置；`PUBLIC_BASE_URL` 与 `DOWNLOAD_DIR` 会显示在 Docker Desktop 的环境变量中，可以直接修改。默认情况下，视频、图片、上传文件、任务数据库和同步密钥都保存在 `DOWNLOAD_DIR`。`--cpus=8` 是 Docker 运行时限制；使用 Compose 时默认也限制为 8 核，可在 `.env` 通过 `CONTAINER_CPUS` 修改。

如果 Windows 目录映射导致 SQLite 查询偏慢，可把运行数据库单独放到 Docker 命名卷，同时继续把媒体文件映射到宿主机：

```bash
docker volume create jable_api_data
docker run -d --name jabletvdownload-api --restart unless-stopped --cpus=8 -p 8000:8000 -e DATABASE_PATH=/data/tasks.sqlite3 -v jable_api_data:/data -v D:\downloads:/downloads jable-downloader
```

第一次启用且 `/data/tasks.sqlite3` 尚不存在时，服务会从 `/downloads/tasks.sqlite3` 自动迁移现有数据库和配套密钥；已存在的卷数据库永远不会被旧文件覆盖。系统配置的“立即备份数据库”使用 SQLite 在线快照把当前数据库覆盖备份到 `/downloads/tasks.sqlite3`。未配置 `DATABASE_PATH` 时，运行数据库仍是 `/downloads/tasks.sqlite3`，按钮改为覆盖 `/downloads/tasks.backup.sqlite3`。也可通过 `DATABASE_BACKUP_PATH` 指定其他备份路径。

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
