"""Run with: python -m uvicorn api:app --host 0.0.0.0 --port 8000."""
import hmac
import logging
import os
import re
import time
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit, parse_qsl

import requests
from fastapi import Body, Depends, FastAPI, File, Form, Query, Security, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware

from config import headers
from video_data import parse_detail, parse_list
import provider_123av
from upload_manager import UploadError, UploadManager

def is_123av_host(hostname):
    return '123av' in (hostname or '').lower()


def is_jable_host(hostname):
    return 'jable' in (hostname or '').lower()


def provider(url):
    return '123av' if is_123av_host(urlsplit(url).hostname) else 'jable'


def list_data(source, url, page):
    if provider(url) == '123av':
        return provider_123av.parse_list(source, url, page)
    result = parse_list(source, url, page)
    result['source'] = 'jable'
    for item in result['items']:
        item['source'] = 'jable'
    return result


def detail_data(source, url):
    if provider(url) == '123av':
        return provider_123av.parse_detail(source, url)
    return dict(parse_detail(source, url), source='jable')
TIMEOUT = float(os.getenv('JABLE_TIMEOUT', '30'))
if TIMEOUT <= 0:
    raise ValueError('JABLE_TIMEOUT must be positive')
API_KEY = os.getenv('JABLE_API_KEY', 'yes@1234') or 'yes@1234'
DOWNLOAD_ROOT = Path(os.getenv('DOWNLOAD_DIR', str(Path(__file__).parent / 'downloads'))).resolve()
LEGACY_DATABASE_PATH = DOWNLOAD_ROOT / 'tasks.sqlite3'
DATABASE_PATH = Path(os.getenv('DATABASE_PATH') or LEGACY_DATABASE_PATH).resolve()
DATABASE_BACKUP_PATH = Path(os.getenv('DATABASE_BACKUP_PATH') or (
    LEGACY_DATABASE_PATH if DATABASE_PATH != LEGACY_DATABASE_PATH
    else DOWNLOAD_ROOT / 'tasks.backup.sqlite3')).resolve()
logger = logging.getLogger(__name__)


class UploadPreviewFiles(StaticFiles):
    """Serve upload files locally and transparently fall back to private R2."""

    def __init__(self, *, directory, relative_root, manager_getter):
        super().__init__(directory=directory)
        self.relative_root = relative_root.strip('/')
        self.manager_getter = manager_getter

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or scope.get('method') not in ('GET', 'HEAD'):
                raise
        clean = unquote(str(path or '')).replace('\\', '/').lstrip('/')
        if not clean or any(part in ('', '.', '..') for part in clean.split('/')):
            raise HTTPException(status_code=404)
        manager = self.manager_getter()
        if manager is None:
            raise HTTPException(status_code=404)
        request_headers = {key.lower(): value for key, value in scope.get('headers', [])}
        byte_range = request_headers.get(b'range')
        try:
            remote = await run_in_threadpool(
                manager.open_preview_object,
                f'{self.relative_root}/{clean}',
                byte_range.decode('latin-1') if byte_range else None,
                scope.get('method') == 'HEAD')
        except FileNotFoundError:
            raise HTTPException(status_code=404) from None
        except ValueError as exc:
            return Response(str(exc), status_code=416,
                            media_type='text/plain; charset=utf-8')
        except Exception as exc:
            logger.exception('Unable to read upload preview from R2')
            return Response('R2 文件读取失败：' + str(exc)[:240], status_code=502,
                            media_type='text/plain; charset=utf-8')
        if scope.get('method') == 'HEAD':
            body = remote.get('body')
            if body is not None:
                body.close()
            return Response(status_code=remote['status'], headers=remote['headers'],
                            media_type=remote['media_type'])

        body = remote['body']

        def stream():
            try:
                for chunk in body.iter_chunks(chunk_size=256 * 1024):
                    if chunk:
                        yield chunk
            finally:
                body.close()

        return StreamingResponse(stream(), status_code=remote['status'],
                                 headers=remote['headers'], media_type=remote['media_type'])


@asynccontextmanager
async def lifespan(app):
    from download_tasks import DownloadManager
    from collection_tasks import CollectionManager
    from download_signers import DownloadSignerManager
    from import_scripts import ImportScriptManager
    from resource_sync import ResourceSyncManager
    from r2_sync import R2SyncManager
    from system_monitor import SystemMonitor
    from local_collectors import LocalCollectorManager
    from player_proxy import PlayerProxyManager
    app.state.downloads = DownloadManager(
        DOWNLOAD_ROOT, start_enabled=False, database=DATABASE_PATH,
        legacy_database=LEGACY_DATABASE_PATH)
    app.state.player_proxy = PlayerProxyManager(app.state.downloads.database)
    app.state.download_signers = DownloadSignerManager(app.state.downloads.database)
    app.state.downloads.signers = app.state.download_signers
    from resource_preview import ResourcePreviewManager
    app.state.resource_preview = ResourcePreviewManager(
        app.state.download_signers, app.state.player_proxy, app.state.downloads, API_KEY)
    app.state.r2_sync = R2SyncManager(DOWNLOAD_ROOT, app.state.downloads.database)
    app.state.downloads.completion_callback = app.state.r2_sync.enqueue_download
    app.state.uploads = UploadManager(
        DOWNLOAD_ROOT, app.state.downloads.database,
        public_base_url_getter=lambda: app.state.downloads.public_base_url,
        completion_callback=app.state.r2_sync.enqueue_upload)
    app.state.r2_sync.request_backfill()
    app.state.system_monitor = SystemMonitor(
        DOWNLOAD_ROOT, app.state.downloads.database,
        runtime_provider=app.state.uploads.ffmpeg_runtime_processes)
    def collect(url):
        source, final_url = fetch_page(url)
        return list_data(source, final_url, list_page(final_url))
    app.state.collections = CollectionManager(
        DOWNLOAD_ROOT, app.state.downloads, collect, auto_start=False)
    app.state.import_scripts = ImportScriptManager(app.state.downloads.database)
    app.state.resource_sync = ResourceSyncManager(
        app.state.downloads.database, app.state.collections.export_items,
        resolve_resource_for_sync)
    app.state.collections.node_enqueue_callback = app.state.resource_sync.enqueue_node_items
    app.state.collections.site_enqueue_callback = app.state.resource_sync.enqueue_site_items
    def download_completed(task, metadata, folder):
        try:
            app.state.r2_sync.enqueue_download(task, metadata, folder)
        except Exception:
            logger.exception('Unable to enqueue completed resource for R2 synchronization')
        try:
            app.state.resource_sync.enqueue_completed_download(task)
        except Exception:
            logger.exception('Unable to enqueue completed resource for website synchronization')
    app.state.downloads.completion_callback = download_completed
    app.state.collections.start()
    app.state.local_collectors = LocalCollectorManager(app.state.downloads.database)
    try:
        yield
    finally:
        app.state.local_collectors.close()
        app.state.system_monitor.close()
        app.state.resource_sync.close()
        await __import__('asyncio').to_thread(app.state.collections.close)
        app.state.uploads.close()
        app.state.r2_sync.close()
        await __import__('asyncio').to_thread(app.state.downloads.close)
        await __import__('asyncio').to_thread(close_browser_session)


app = FastAPI(title='JableTVDownload API', version='1.3.0', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=['*'],
                   allow_methods=['*'], allow_headers=['Range', 'Content-Type', 'X-API-Key', 'X-Collector-Token'],
                   expose_headers=['Accept-Ranges', 'Content-Length', 'Content-Range',
                                   'Server-Timing'])
for public_path, relative_path in (
        ('/jable', 'jable'), ('/123', '123'), ('/external', 'external')):
    static_directory = DOWNLOAD_ROOT / relative_path
    static_directory.mkdir(parents=True, exist_ok=True)
    app.mount(public_path, StaticFiles(directory=static_directory),
              name='media-' + public_path.lstrip('/'))
for category in ('video', 'image', 'other'):
    relative_path = f'uploads/{category}'
    static_directory = DOWNLOAD_ROOT / relative_path
    static_directory.mkdir(parents=True, exist_ok=True)
    app.mount(f'/uploads/{category}', UploadPreviewFiles(
        directory=static_directory, relative_root=relative_path,
        manager_getter=lambda: getattr(app.state, 'r2_sync', None)),
        name='media-uploads-' + category)
key_header = APIKeyHeader(name='X-API-Key', auto_error=False)
browser_slot = threading.BoundedSemaphore(1)
browser_state_lock = threading.RLock()
browser_driver = None
browser_cookies = ()
browser_user_agent = None


def _discard_browser():
    global browser_driver
    current, browser_driver = browser_driver, None
    if current is not None:
        try:
            current.quit()
        except Exception:
            pass


def _persistent_browser():
    global browser_driver
    from crawler import initWebDriver
    if browser_driver is not None:
        try:
            browser_driver.execute_script('return 1')
            return browser_driver
        except Exception:
            _discard_browser()
    browser_driver = initWebDriver()
    browser_driver.set_page_load_timeout(TIMEOUT)
    return browser_driver


def _remember_browser_state(driver):
    global browser_cookies, browser_user_agent
    try:
        cookies = tuple(driver.get_cookies())
        user_agent = driver.execute_script('return navigator.userAgent')
    except Exception:
        return
    with browser_state_lock:
        browser_cookies = cookies
        browser_user_agent = str(user_agent or '') or None


def _apply_browser_state(session, url):
    hostname = urlsplit(url).hostname or ''
    with browser_state_lock:
        cookies, user_agent = browser_cookies, browser_user_agent
    if user_agent:
        session.headers['User-Agent'] = user_agent
    for cookie in cookies:
        domain = str(cookie.get('domain') or hostname)
        if hostname != domain.lstrip('.') and not hostname.endswith('.' + domain.lstrip('.')):
            continue
        try:
            session.cookies.set(cookie['name'], cookie['value'], domain=domain,
                                path=cookie.get('path') or '/')
        except (KeyError, TypeError, ValueError):
            continue


def close_browser_session():
    if browser_slot.acquire(timeout=5):
        try:
            _discard_browser()
        finally:
            browser_slot.release()


def fetch_browser_page(url, detail=False):
    """Reuse one verified browser when HTTP receives a challenge."""
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.support.ui import WebDriverWait

    url = validate_url(url, detail)
    if not browser_slot.acquire(timeout=TIMEOUT):
        raise APIError(503, 50301, '浏览器抓取繁忙，请稍后重试')
    try:
        driver = _persistent_browser()
        driver.get(url)
        def ready(browser):
            validate_url(browser.current_url, detail)
            source = browser.page_source
            return ((provider_123av.player_urls(source) if provider(url) == '123av'
                     else parse_detail(source, browser.current_url)['m3u8_url']) if detail
                    else list_data(source, browser.current_url, 1)['items'])
        WebDriverWait(driver, TIMEOUT).until(ready)
        final_url = validate_url(driver.current_url, detail)
        _remember_browser_state(driver)
        return driver.page_source, final_url
    except TimeoutException:
        _discard_browser()
        raise APIError(502, 50203, '浏览器未能加载视频数据，来源可能仍要求验证或页面结构已变化')
    except WebDriverException:
        _discard_browser()
        raise APIError(502, 50204, '浏览器抓取失败，请检查 Chromium 和 ChromeDriver 安装及服务日志')
    finally:
        browser_slot.release()


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


@app.exception_handler(UploadError)
async def upload_error_handler(request, exc):
    return JSONResponse(status_code=exc.status,
                        content=dict(code=exc.code, message=exc.message, data=exc.data))


class Video(BaseModel):
    source: str = 'jable'
    id: str
    title: str | None = None
    url: str
    cover: str | None = None
    preview_image: str | None = None
    duration: str | None = None


class Detail(Video):
    poster: str | None = None
    has_vtt: bool | None = None
    vtt_url: str | None = None
    published_at: str | None = None
    actors: list[str]
    tags: list[str]
    m3u8_url: str
    chapters: list[dict] = Field(default_factory=list)


class Page(BaseModel):
    source: str = 'jable'
    page: int
    has_next: bool
    items: list[Video]


class ListResponse(BaseModel):
    code: int = 0
    message: str = 'ok'
    data: Page


class DetailResponse(BaseModel):
    code: int = 0
    message: str = 'ok'
    data: Detail


def error_response(status, code, message):
    return JSONResponse(status_code=status, content=dict(code=code, message=message, data=None))


@app.exception_handler(APIError)
async def api_error_handler(request, exc):
    return error_response(exc.status, exc.code, exc.message)


@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc):
    return error_response(400, 40001, '参数错误，请检查查询参数、表单字段或请求 JSON 格式')


@app.exception_handler(HTTPException)
async def http_error_handler(request, exc):
    return error_response(exc.status_code, exc.status_code * 100, str(exc.detail))


@app.exception_handler(Exception)
async def unexpected_error_handler(request, exc):
    logger.error('API request failed', exc_info=(type(exc), exc, exc.__traceback__))
    return error_response(500, 50000, '服务内部错误')


def authorize(key: str | None = Security(key_header)):
    if not key or not hmac.compare_digest(key.encode(), API_KEY.encode()):
        raise APIError(401, 40101, 'API Key 无效或缺失')


@app.get('/api/auth/check', dependencies=[Depends(authorize)])
def auth_check():
    return dict(code=0, message='ok', data=dict(authenticated=True))


@app.get('/api/system/metrics', dependencies=[Depends(authorize)])
def system_metrics(request: Request):
    return dict(code=0, message='ok', data=request.app.state.system_monitor.current())


@app.get('/api/system/metrics/history', dependencies=[Depends(authorize)])
def system_metrics_history(request: Request,
                           minutes: int = Query(60, ge=1, le=4320)):
    return dict(
        code=0, message='ok', data=request.app.state.system_monitor.history(minutes))


@app.get('/api/system/ffmpeg-processes', dependencies=[Depends(authorize)])
def system_ffmpeg_processes(request: Request):
    return dict(
        code=0, message='ok',
        data={'items': request.app.state.system_monitor.ffmpeg_processes()})


def media_signature(task_id, expires):
    return hmac.new(API_KEY.encode(), f'{task_id}:{expires}'.encode(), 'sha256').hexdigest()


def authorize_media(task_id: str, token: str | None = Query(None), key: str | None = Security(key_header)):
    if not API_KEY or (key and hmac.compare_digest(key.encode(), API_KEY.encode())):
        return
    try:
        expires_text, signature = (token or '').split('.', 1)
        expires = int(expires_text)
        valid = expires >= int(time.time()) and expires <= int(time.time()) + 86400
        valid = valid and hmac.compare_digest(signature, media_signature(task_id, expires))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise APIError(401, 40101, '媒体令牌无效或已过期')


def validate_url(url, detail=False, list_search=False):
    try:
        parts = urlsplit(url)
        av123 = is_123av_host(parts.hostname)
        valid = (parts.scheme in ('http', 'https') and bool(parts.hostname)
                 and (av123 or is_jable_host(parts.hostname))
                 and not parts.username and not parts.password
                 and not re.search(r'[\s\\\x00-\x1f]', url))
        parts.port
    except ValueError:
        valid = False
    if not valid:
        raise APIError(400, 40002, '地址必须使用 HTTP/HTTPS，且域名包含 jable 或 123av')
    return urlunsplit((parts.scheme, parts.netloc, parts.path or '/', parts.query, ''))


def list_page(url):
    if provider(url) == '123av':
        page = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True)).get('page', '1')
        return int(page) if re.fullmatch(r'[1-9][0-9]{0,8}', page) else 1
    match = re.search(r'/latest-updates/(\d+)/', urlsplit(url).path)
    return int(match.group(1)) if match else 1


def resolve_player(url, preview=None):
    """Read resource URLs from the ordinary embedded player, not arbitrary pages."""
    from crawler import initWebDriver
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.support.ui import WebDriverWait

    def check(target):
        try:
            p = urlsplit(target)
            valid = (p.scheme == 'https' and p.hostname == 'javplayer.cc' and p.port in (None, 443)
                     and not p.username and not p.password and not p.fragment
                     and not re.search(r'[\s\\\x00-\x1f]', target)
                     and re.fullmatch(r'/e/[A-Za-z0-9]+', p.path))
        except ValueError:
            valid = False
        if not valid:
            raise APIError(502, 50202, '来源返回了不支持的播放器地址')
        # Poster is decorative; do not forward nested URL parameters to the browser.
        return urlunsplit(('https', p.hostname, p.path, '', ''))
    url = check(url)
    if not browser_slot.acquire(timeout=TIMEOUT):
        raise APIError(503, 50301, '浏览器抓取繁忙，请稍后重试')
    driver = None
    try:
        driver = initWebDriver()
        driver.set_page_load_timeout(TIMEOUT)
        driver.get(url)
        def media(browser):
            check(browser.current_url)
            resources = browser.execute_script("return performance.getEntriesByType('resource').map(x=>x.name)")
            return next((u for u in resources if urlsplit(u).scheme in ('http', 'https')
                         and urlsplit(u).path.endswith('.m3u8')), None)
        stream_url = WebDriverWait(driver, TIMEOUT).until(media)
        if preview is not None:
            def thumbnail_index(browser):
                check(browser.current_url)
                resources = browser.execute_script("return performance.getEntriesByType('resource').map(x=>x.name)")
                return next((u for u in resources if urlsplit(u).scheme in ('http', 'https')
                             and urlsplit(u).path.lower().endswith('.vtt')), None)
            try:
                # Preview metadata may load just after the HLS manifest.
                vtt_url = WebDriverWait(driver, min(5, TIMEOUT)).until(thumbnail_index)
            except TimeoutException:
                vtt_url = None
            preview['vtt_url'] = vtt_url
            preview['has_vtt'] = True if vtt_url else None
        return stream_url
    except TimeoutException:
        raise APIError(502, 50202, '播放器未返回 m3u8 地址或访问受限')
    except WebDriverException:
        raise APIError(502, 50204, '播放器浏览器启动或加载失败')
    finally:
        try:
            if driver is not None:
                driver.quit()
        finally:
            browser_slot.release()


def fetch_page(url, detail=False):
    """Validate each hop before connecting; never follow arbitrary redirects."""
    url = validate_url(url, detail)
    deadline = time.monotonic() + TIMEOUT
    try:
        with requests.Session() as session:
            session.headers.update(headers)
            _apply_browser_state(session, url)
            for _ in range(6):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise requests.Timeout()
                with session.get(url, timeout=remaining, allow_redirects=False, stream=True) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        target = response.headers.get('Location')
                        try:
                            if not target:
                                raise ValueError()
                            url = validate_url(urljoin(url, target), detail)
                        except (APIError, ValueError):
                            raise APIError(502, 50201, '来源网站返回了不允许的重定向')
                        continue
                    if response.status_code == 404:
                        raise APIError(404, 40401, '页面不存在')
                    if response.status_code != 200:
                        logger.warning('Upstream HTTP %s (browser challenge: %s)', response.status_code,
                                       response.headers.get('cf-mitigated') == 'challenge')
                        if response.headers.get('cf-mitigated') == 'challenge':
                            return fetch_browser_page(url, detail)
                        raise APIError(502, 50201, f'来源网站访问失败（HTTP {response.status_code}）')
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        if time.monotonic() > deadline:
                            raise requests.Timeout()
                        size += len(chunk)
                        if size > 5 * 1024 * 1024:
                            raise APIError(502, 50201, '来源页面过大')
                        chunks.append(chunk)
                    return b''.join(chunks).decode('utf-8', errors='replace'), url
            raise APIError(502, 50201, '来源网站重定向次数过多')
    except requests.Timeout:
        raise APIError(504, 50401, '来源网站抓取超时')
    except requests.RequestException as exc:
        # Do not log exception text: proxy URLs may contain credentials.
        logger.warning('Upstream connection failed: %s', type(exc).__name__)
        if isinstance(exc, requests.exceptions.ProxyError):
            message = '代理连接失败，请检查 API 运行环境的代理配置'
        elif isinstance(exc, requests.exceptions.SSLError):
            message = '来源网站 TLS 连接或证书校验失败'
        else:
            message = '来源网站连接失败，请检查 API 运行环境的网络和代理配置'
        raise APIError(502, 50201, message)


@app.get('/api/list', response_model=ListResponse, dependencies=[Depends(authorize)])
def latest(page: int = Query(1, ge=1), url: str | None = Query(None, max_length=2048, description='完整列表页链接；传入时以链接中的分页为准')):
    if url is not None:
        url = validate_url(url)
        if provider(url) == '123av':
            page = list_page(url)
        else:
            match = re.search(r'/latest-updates/(\d+)/', urlsplit(url).path)
            page = int(match.group(1)) if match else 1
    else:
        url = 'https://jable.tv/latest-updates/' + (f'{page}/' if page > 1 else '')
    source, final_url = fetch_page(url)
    result = list_data(source, final_url, page)
    if not result['items']:
        raise APIError(502, 50202, '未解析到视频列表，页面结构可能变化或访问受限')
    return dict(code=0, message='ok', data=result)


@app.get('/api/detail', response_model=DetailResponse, dependencies=[Depends(authorize)])
def detail(url: str = Query(..., max_length=2048)):
    return dict(code=0, message='ok', data=resolve_detail(url))


def resolve_detail(url):
    source, final_url = fetch_page(url, detail=True)
    result = detail_data(source, final_url)
    if provider(final_url) == '123av':
        chapters = result.get('chapters') or []
        resolved = []
        for chapter in chapters:
            current = dict(chapter)
            preview = {}
            current['m3u8_url'] = resolve_player(current.pop('player_url'), preview=preview)
            current['vtt_url'] = preview.get('vtt_url')
            current['has_vtt'] = preview.get('has_vtt')
            resolved.append(current)
        result['chapters'] = resolved
        if resolved:
            result['m3u8_url'] = resolved[0]['m3u8_url']
            result['vtt_url'] = resolved[0].get('vtt_url')
            result['has_vtt'] = resolved[0].get('has_vtt')
    if not result['m3u8_url']:
        raise APIError(502, 50202, '未解析到播放地址，页面结构可能变化或访问受限')
    return result


def resolve_resource_for_sync(item):
    """Resolve fresh remote media locally before handing a resource to a download node."""
    if item.get('source') not in ('jable', '123av') and item.get('m3u8_url'):
        return item
    url = item.get('url')
    if not url:
        if item.get('m3u8_url'):
            return item
        raise APIError(400, 40002, '资源缺少详情页地址，无法在本地解析播放链接')
    detail = resolve_detail(url)
    for field in ('title', 'cover', 'poster', 'preview_image', 'vtt_url', 'duration',
                  'published_at', 'actors', 'tags', 'm3u8_url', 'chapters'):
        if detail.get(field) not in (None, '', []):
            item[field] = detail[field]
    return item


class DownloadInput(BaseModel):
    url: str = Field(min_length=1, max_length=2048, description='Jable 或 123AV 视频详情链接')


class DownloadTask(BaseModel):
    task_id: str
    source: str
    video_id: str
    url: str
    status: str
    r2_synced: bool = False
    r2_sync_status: str = 'unsynced'
    r2_synced_at: float | None = None
    local_deleted: bool = False
    local_deleted_at: float | None = None
    downloaded_segments: int
    total_segments: int
    downloaded_bytes: int
    progress: float
    save_path: str
    playlist_path: str | None
    title: str | None = None
    poster_file: str | None = None
    cover_url: str | None = None
    preview_file: str | None = None
    preview_url: str | None = None
    vtt_file: str | None = None
    vtt_url: str | None = None
    m3u8_url: str | None = None
    preview_m3u8_file: str | None = None
    preview_m3u8_url: str | None = None
    preview5_m3u8_file: str | None = None
    preview5_m3u8_url: str | None = None
    published_at: str | None = None
    actors: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    chapters: list[dict] = Field(default_factory=list)
    message: str | None = None
    error: str | None
    warnings: list[str]
    created_at: float
    updated_at: float


class DownloadResponse(BaseModel):
    code: int = 0
    message: str = 'ok'
    data: DownloadTask


class DeleteDownloadData(BaseModel):
    task_id: str
    deleted: bool
    deleted_path: str


class DeleteDownloadResponse(BaseModel):
    code: int = 0
    message: str = 'deleted'
    data: DeleteDownloadData


class DownloadListData(BaseModel):
    total: int
    items: list[DownloadTask]


class DownloadListResponse(BaseModel):
    code: int = 0
    message: str = 'ok'
    data: DownloadListData


@app.post('/api/download', response_model=DownloadResponse, dependencies=[Depends(authorize)])
def start_download(body: DownloadInput, request: Request):
    url = validate_url(body.url, detail=True)
    site = provider(url)
    video_id = urlsplit(url).path.rstrip('/').split('/')[-1]
    result = request.app.state.downloads.submit(site, video_id, url)
    return dict(code=0, message='ok', data=result)


@app.get('/api/download/{task_id}', response_model=DownloadResponse, dependencies=[Depends(authorize)])
def download_progress(task_id: str, request: Request):
    result = request.app.state.downloads.get(task_id)
    if result is None:
        raise APIError(404, 40402, '下载任务不存在')
    return dict(code=0, message='ok', data=result)


@app.get('/api/downloads', response_model=DownloadListResponse, dependencies=[Depends(authorize)])
def download_list(request: Request, status: str | None = Query(None), search: str | None = Query(None, max_length=100),
                  limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    if status and status not in {'pending', 'waiting_collector', 'queued', 'downloading', 'completed', 'failed', 'deleting'}:
        raise APIError(400, 40001, '下载状态参数无效')
    total, items = request.app.state.downloads.list(status, search, limit, offset)
    return dict(code=0, message='ok', data=dict(total=total, items=items))


@app.post('/api/downloads/deduplicate', dependencies=[Depends(authorize)])
def deduplicate_downloads(request: Request):
    result = request.app.state.downloads.deduplicate()
    return dict(code=0, message='ok', data=result)


@app.delete('/api/download/{task_id}', response_model=DeleteDownloadResponse, dependencies=[Depends(authorize)])
def delete_download(task_id: str, request: Request):
    if not re.fullmatch(r'[a-f0-9]{32}',task_id):
        raise APIError(404,40402,'下载任务不存在')
    try:
        result = request.app.state.downloads.delete(task_id)
    except __import__('download_tasks').DownloadError as exc:
        raise APIError(409,40901,str(exc))
    if result is None:
        raise APIError(404,40402,'下载任务不存在')
    return dict(code=0,message='deleted',data=result)


@app.post('/api/download/{task_id}/media-token', dependencies=[Depends(authorize)])
def media_token(task_id: str, request: Request):
    if request.app.state.downloads.get(task_id) is None:
        raise APIError(404, 40402, '下载任务不存在')
    expires = int(time.time()) + 21600
    token = f'{expires}.{media_signature(task_id, expires)}' if API_KEY else ''
    return dict(code=0, message='ok', data=dict(token=token, expires_at=expires))


@app.get('/api/download/{task_id}/files/{relative:path}', dependencies=[Depends(authorize)])
def download_file(task_id: str, relative: str, request: Request, token: str | None = Query(None)):
    if not re.fullmatch(r'[a-f0-9]{32}', task_id):
        raise APIError(404, 40402, '下载任务不存在')
    target = request.app.state.downloads.media_path(task_id, relative)
    if target is None:
        raise APIError(404, 40403, '资源文件不存在')
    media_types = {'.m3u8': 'application/vnd.apple.mpegurl', '.ts': 'video/mp2t',
                   '.vtt': 'text/vtt', '.jpg': 'image/jpeg', '.png': 'image/png',
                   '.mp4': 'video/mp4', '.webm': 'video/webm'}
    headers = {'Cache-Control': 'private, max-age=3600'}
    if target.suffix.lower() == '.vtt' and token:
        content = target.read_text(encoding='utf-8-sig')
        rewritten = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith(('WEBVTT', 'NOTE', 'STYLE', 'REGION')) and '-->' not in stripped:
                before, separator, fragment = line.partition('#')
                joiner = '&' if '?' in before else '?'
                line = before + joiner + 'token=' + token + (separator + fragment if separator else '')
            rewritten.append(line)
        return Response('\n'.join(rewritten), media_type='text/vtt', headers=headers)
    return FileResponse(target, media_type=media_types.get(target.suffix.lower()), headers=headers)


class CollectionApiOptions(BaseModel):
    collection_type: str = Field('webpage', pattern='^(webpage|api)$', description='网页列表或JSON接口')
    api_source: str = Field('', max_length=100, description='响应缺少source时使用的默认来源')
    request_method: str = Field('GET', pattern='^(GET|POST)$')
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: object | None = None
    request_timeout: int = Field(30, ge=5, le=120)


class CollectionInput(CollectionApiOptions):
    name: str | None = Field(None, max_length=80)
    url: str = Field(min_length=1, max_length=8192, description='网页列表地址或HTTP/HTTPS JSON接口')
    batch_size: int = Field(1, ge=1, le=20, description='兼容字段；列表采集始终保存当前页全部数据')
    scheduled_enabled: bool = Field(False, description='每天北京时间 18:00 自动采集')
    r2_sync_enabled: bool = Field(False, description='资源下载完成后自动同步到R2')
    local_collect_enabled: bool = Field(False, description='列表和资源详情交给采集客户端解析')
    auto_download_enabled: bool = Field(False, description='列表采集完成后立即加入下载调度')
    node_ids: list[str] = Field(default_factory=list, max_length=20,
                                description='采集成功后立即调度到这些下载节点')
    sync_site_ids: list[str] = Field(default_factory=list, max_length=20,
                                     description='资源下载完成后自动发送到这些同步网站')


class CollectionBulkInput(CollectionApiOptions):
    name: str | None = Field(None, max_length=80)
    url_template: str = Field(min_length=1, max_length=8192,
                              description='网页列表需包含 ${page}；API 地址也可在 POST 请求 JSON 中使用变量')
    start_page: int = Field(ge=1, le=100000)
    end_page: int = Field(ge=1, le=100000)
    scheduled_enabled: bool = Field(False, description='每天北京时间 18:00 自动采集')
    r2_sync_enabled: bool = Field(False, description='资源下载完成后自动同步到R2')
    local_collect_enabled: bool = Field(False, description='列表和资源详情交给采集客户端解析')
    auto_download_enabled: bool = Field(False, description='列表采集完成后立即加入下载调度')
    node_ids: list[str] = Field(default_factory=list, max_length=20,
                                description='所有批量任务共用的下载节点')
    sync_site_ids: list[str] = Field(default_factory=list, max_length=20,
                                     description='所有批量任务共用的同步网站')


class CollectionTask(BaseModel):
    id: str
    name: str
    url: str
    batch_size: int
    collection_type: str = 'webpage'
    api_source: str = ''
    request_method: str = 'GET'
    request_headers: dict = Field(default_factory=dict)
    request_body: object | None = None
    request_timeout: int = 30
    last_updated: int = 0
    last_failed: int = 0
    scheduled_enabled: bool = False
    r2_sync_enabled: bool = False
    local_collect_enabled: bool = False
    auto_download_enabled: bool = False
    node_ids: list[str] = Field(default_factory=list)
    sync_site_ids: list[str] = Field(default_factory=list)
    sync_sites: list[dict] = Field(default_factory=list)
    last_scheduled_date: str | None = None
    status: str
    last_total: int
    last_created: int
    last_skipped: int
    last_node_queued: int = 0
    last_node_skipped: int = 0
    last_node_failed: int = 0
    last_download_queued: int = 0
    last_download_skipped: int = 0
    last_download_failed: int = 0
    last_task_ids: list[str]
    last_error: str | None
    last_run_at: float | None
    created_at: float
    updated_at: float


def collection_url(url, collection_type='webpage'):
    if collection_type == 'api':
        from api_collection import validate_api_url, ApiCollectionError
        try:
            return validate_api_url(url)
        except ApiCollectionError as exc:
            raise APIError(400, 40007, str(exc)) from None
    return validate_url(url)


def collection_api_options(body):
    from api_collection import validate_options, ApiCollectionError
    try:
        return validate_options(**body.model_dump(include=set(CollectionApiOptions.model_fields)))
    except ApiCollectionError as exc:
        raise APIError(400, 40007, str(exc)) from None


def contains_page_template(value):
    if isinstance(value, str):
        return '${page}' in value
    if isinstance(value, list):
        return any(contains_page_template(item) for item in value)
    if isinstance(value, dict):
        return any(contains_page_template(item) for item in value.values())
    return False


def collection_node_ids(request, node_ids):
    from resource_sync import SyncError
    try:
        return request.app.state.resource_sync.validate_node_ids(node_ids)
    except SyncError as exc:
        raise APIError(400, 40031, str(exc)) from None


def collection_site_ids(request, site_ids):
    from resource_sync import SyncError
    try:
        return request.app.state.resource_sync.validate_site_ids(site_ids)
    except SyncError as exc:
        raise APIError(400, 40032, str(exc)) from None


class CollectorNodeInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True


class CollectorLeaseInput(BaseModel):
    lease_token: str = Field(min_length=1, max_length=80)


class CollectorCompleteInput(CollectorLeaseInput):
    result: dict


class CollectorFailInput(CollectorLeaseInput):
    error: str = Field(min_length=1, max_length=500)


class LocalCollectorInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    server_url: str = Field(min_length=1, max_length=2048)
    token: str | None = Field(default=None, min_length=22, max_length=22)
    concurrency: int = Field(default=1, ge=1, le=10)
    auto_start: bool = False


class LocalCollectorIdsInput(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)


collector_token_header = APIKeyHeader(name='X-Collector-Token', auto_error=False)


def authorize_collector(request: Request, token: str | None = Security(collector_token_header)):
    node = request.app.state.collections.authenticate_collector(token)
    if node is None:
        raise APIError(401, 40140, '采集节点Token无效')
    return node


@app.get('/api/collector/check')
def collector_check(request: Request, node: dict = Depends(authorize_collector)):
    request.app.state.collections.heartbeat_collector(node['id'])
    return dict(code=0, message='ok', data=node)


@app.get('/api/collector-nodes', dependencies=[Depends(authorize)])
def collector_node_list(request: Request):
    return dict(code=0, message='ok', data=request.app.state.collections.list_collector_nodes(include_token=True))


@app.post('/api/collector-nodes', dependencies=[Depends(authorize)])
def collector_node_create(body: CollectorNodeInput, request: Request):
    from collection_tasks import CollectionError
    try:
        result = request.app.state.collections.create_collector_node(body.name, body.enabled)
    except CollectionError as exc:
        raise APIError(400, 40040, str(exc)) from None
    return dict(code=0, message='ok', data=result)


@app.put('/api/collector-nodes/{node_id}', dependencies=[Depends(authorize)])
def collector_node_update(node_id: str, body: CollectorNodeInput, request: Request):
    from collection_tasks import CollectionError
    try:
        result = request.app.state.collections.update_collector_node(node_id, body.name, body.enabled)
    except CollectionError as exc:
        raise APIError(400, 40040, str(exc)) from None
    if not result:
        raise APIError(404, 40440, '采集节点不存在')
    return dict(code=0, message='ok', data=dict(id=node_id))


@app.post('/api/collector-nodes/{node_id}/rotate-token', dependencies=[Depends(authorize)])
def collector_node_rotate(node_id: str, request: Request):
    result = request.app.state.collections.rotate_collector_token(node_id)
    if not result:
        raise APIError(404, 40440, '采集节点不存在')
    return dict(code=0, message='ok', data=result)


@app.delete('/api/collector-nodes/{node_id}', dependencies=[Depends(authorize)])
def collector_node_delete(node_id: str, request: Request):
    if not request.app.state.collections.delete_collector_node(node_id):
        raise APIError(404, 40440, '采集节点不存在')
    return dict(code=0, message='ok', data=dict(id=node_id))


@app.get('/api/collector/nodes', dependencies=[Depends(authorize)])
def collector_nodes(request: Request):
    return dict(code=0, message='ok', data=request.app.state.collections.list_collector_nodes())


@app.post('/api/collector/jobs/claim')
def collector_claim(request: Request, node: dict = Depends(authorize_collector)):
    from collection_tasks import CollectionError
    try:
        job = request.app.state.collections.claim_collector_job(
            node['id'])
    except CollectionError as exc:
        raise APIError(400, 40040, str(exc)) from None
    return dict(code=0, message='ok', data=job)


@app.post('/api/collector/jobs/{job_id}/heartbeat')
def collector_heartbeat(job_id: str, body: CollectorLeaseInput, request: Request,
                        node: dict = Depends(authorize_collector)):
    if not request.app.state.collections.heartbeat_collector(
            node['id'], job_id, body.lease_token):
        raise APIError(409, 40940, '采集任务租约已失效')
    return dict(code=0, message='ok', data=dict(job_id=job_id, active=True))


@app.post('/api/collector/jobs/{job_id}/complete')
def collector_complete(job_id: str, body: CollectorCompleteInput, request: Request,
                       node: dict = Depends(authorize_collector)):
    from collection_tasks import CollectionError
    try:
        result = request.app.state.collections.complete_collector_job(
            job_id, node['id'], body.lease_token, body.result)
    except CollectionError as exc:
        raise APIError(409, 40940, str(exc)) from None
    return dict(code=0, message='ok', data=result)


@app.post('/api/collector/jobs/{job_id}/fail')
def collector_fail(job_id: str, body: CollectorFailInput, request: Request,
                   node: dict = Depends(authorize_collector)):
    if not request.app.state.collections.fail_collector_job(
            job_id, node['id'], body.lease_token, body.error):
        raise APIError(409, 40940, '采集任务租约已失效')
    return dict(code=0, message='ok', data=dict(job_id=job_id, failed=True))


@app.get('/api/local-collectors', dependencies=[Depends(authorize)])
def local_collector_list(request: Request):
    return dict(code=0, message='ok', data=request.app.state.local_collectors.list())


@app.post('/api/local-collectors', dependencies=[Depends(authorize)])
def local_collector_create(body: LocalCollectorInput, request: Request):
    from local_collectors import LocalCollectorError
    try:
        result = request.app.state.local_collectors.create(body.model_dump())
    except LocalCollectorError as exc:
        raise APIError(400, 40041, str(exc)) from None
    return dict(code=0, message='saved', data=result)


@app.put('/api/local-collectors/{config_id}', dependencies=[Depends(authorize)])
def local_collector_update(config_id: str, body: LocalCollectorInput, request: Request):
    from local_collectors import LocalCollectorError
    try:
        result = request.app.state.local_collectors.update(config_id, body.model_dump())
    except LocalCollectorError as exc:
        raise APIError(409, 40941, str(exc)) from None
    if result is None:
        raise APIError(404, 40441, '采集客户端不存在')
    return dict(code=0, message='saved', data=result)


@app.delete('/api/local-collectors/{config_id}', dependencies=[Depends(authorize)])
def local_collector_delete(config_id: str, request: Request):
    from local_collectors import LocalCollectorError
    try:
        deleted = request.app.state.local_collectors.delete(config_id)
    except LocalCollectorError as exc:
        raise APIError(409, 40941, str(exc)) from None
    if not deleted:
        raise APIError(404, 40441, '采集客户端不存在')
    return dict(code=0, message='deleted', data=dict(id=config_id, deleted=True))


@app.post('/api/local-collectors/{config_id}/test', dependencies=[Depends(authorize)])
def local_collector_test(config_id: str, request: Request):
    from local_collectors import LocalCollectorError
    try:
        result = request.app.state.local_collectors.test(config_id)
    except LocalCollectorError as exc:
        raise APIError(400, 40041, str(exc)) from None
    if result is None:
        raise APIError(404, 40441, '采集客户端不存在')
    return dict(code=0, message='connected', data=result)


@app.post('/api/local-collectors/{config_id}/start', dependencies=[Depends(authorize)])
def local_collector_start(config_id: str, request: Request):
    from local_collectors import LocalCollectorError
    try:
        result = request.app.state.local_collectors.start(config_id)
    except LocalCollectorError as exc:
        raise APIError(409, 40941, str(exc)) from None
    if result is None:
        raise APIError(404, 40441, '采集客户端不存在')
    return dict(code=0, message='started', data=result)


@app.post('/api/local-collectors/{config_id}/stop', dependencies=[Depends(authorize)])
def local_collector_stop(config_id: str, request: Request):
    result = request.app.state.local_collectors.stop(config_id)
    if result is None:
        raise APIError(404, 40441, '采集客户端不存在')
    return dict(code=0, message='stopping', data=result)


@app.post('/api/local-collectors/bulk-start', dependencies=[Depends(authorize)])
def local_collector_bulk_start(body: LocalCollectorIdsInput, request: Request):
    return dict(code=0, message='started',
                data=request.app.state.local_collectors.bulk(body.ids, 'start'))


@app.post('/api/local-collectors/bulk-stop', dependencies=[Depends(authorize)])
def local_collector_bulk_stop(body: LocalCollectorIdsInput, request: Request):
    return dict(code=0, message='stopping',
                data=request.app.state.local_collectors.bulk(body.ids, 'stop'))


@app.get('/api/collections', dependencies=[Depends(authorize)])
def collection_list(request: Request, page: int = Query(1, ge=1),
                    page_size: int = Query(20, ge=1, le=100),
                    keyword: str | None = Query(None, max_length=2048),
                    status: str | None = Query(None),
                    scheduled_enabled: bool | None = Query(None),
                    source: str | None = Query(None)):
    if status and status not in {'idle', 'queued', 'running', 'waiting_collector'}:
        raise APIError(400, 40001, '采集任务状态筛选参数无效')
    if source and source not in {'jable', '123av', 'other'}:
        raise APIError(400, 40001, '采集任务来源筛选参数无效')
    result = request.app.state.collections.list(
        page, page_size, keyword.strip() if keyword else None,
        status, scheduled_enabled, source)
    request.app.state.resource_sync.attach_collection_destinations(result['items'])
    return dict(code=0, message='ok', data=result)


@app.get('/api/collections/options', dependencies=[Depends(authorize)])
def collection_options(request: Request):
    """Return all data needed by the create/edit collection dialog."""
    return dict(code=0, message='ok', data=dict(
        r2_enabled=bool(request.app.state.r2_sync.settings().get('enabled')),
        collector_nodes=request.app.state.collections.list_collector_nodes(),
        sync_sites=request.app.state.resource_sync.list_sites(),
        download_signers=request.app.state.download_signers.list_options()))


@app.post('/api/collections', dependencies=[Depends(authorize)])
def collection_create(body: CollectionInput, request: Request):
    if body.r2_sync_enabled and not request.app.state.r2_sync.settings()['enabled']:
        raise APIError(400, 40030, '请先配置并启用R2')
    url = collection_url(body.url, body.collection_type)
    name = (body.name or '').strip() or ('API接口采集' if body.collection_type=='api' else f'{provider(url)} 列表采集')
    node_ids = collection_node_ids(request, body.node_ids)
    return dict(code=0, message='ok', data=request.app.state.collections.create(
        name, url, body.batch_size, body.scheduled_enabled, body.r2_sync_enabled,
        node_ids, body.local_collect_enabled, body.auto_download_enabled,
        collection_site_ids(request, body.sync_site_ids), **collection_api_options(body)))


@app.post('/api/collections/bulk-create', dependencies=[Depends(authorize)])
def collection_bulk_create(body: CollectionBulkInput, request: Request):
    if body.r2_sync_enabled and not request.app.state.r2_sync.settings()['enabled']:
        raise APIError(400, 40030, '请先配置并启用R2')
    url_has_page = '${page}' in body.url_template
    body_has_page = (body.collection_type == 'api' and body.request_method == 'POST'
                     and contains_page_template(body.request_body))
    if not url_has_page and not body_has_page:
        raise APIError(400, 40007, '批量任务的链接或请求 JSON 至少一处必须包含 ${page}')
    if body.end_page < body.start_page:
        raise APIError(400, 40007, '结束页不能小于起始页')
    if body.end_page - body.start_page + 1 > 500:
        raise APIError(400, 40007, '单次最多批量添加 500 页')
    base_name = (body.name or '').strip()
    node_ids = collection_node_ids(request, body.node_ids)
    tasks = []
    for page in range(body.start_page, body.end_page + 1):
        url = collection_url(body.url_template.replace('${page}', str(page)), body.collection_type)
        prefix = base_name or ('API接口采集' if body.collection_type=='api' else f'{provider(url)} 列表采集')
        tasks.append(dict(page=page, name=f'{prefix} - 第 {page} 页', url=url))
    result = request.app.state.collections.create_many(
        tasks, body.scheduled_enabled, body.r2_sync_enabled, node_ids,
        body.local_collect_enabled, body.auto_download_enabled,
        collection_site_ids(request, body.sync_site_ids), **collection_api_options(body))
    return dict(code=0, message='ok', data=result)


@app.put('/api/collections/{task_id}', dependencies=[Depends(authorize)])
def collection_update(task_id: str, body: CollectionInput, request: Request):
    from collection_tasks import CollectionError
    try:
        if body.r2_sync_enabled and not request.app.state.r2_sync.settings()['enabled']:
            raise APIError(400, 40030, '请先配置并启用R2')
        result = request.app.state.collections.update(task_id, (body.name or '').strip() or '列表采集',
                                                      collection_url(body.url, body.collection_type), body.batch_size,
                                                      body.scheduled_enabled,
                                                      body.r2_sync_enabled,
                                                      collection_node_ids(request, body.node_ids),
                                                      body.local_collect_enabled,
                                                      body.auto_download_enabled,
                                                      collection_site_ids(request,
                                                                          body.sync_site_ids), **collection_api_options(body))
    except CollectionError as exc:
        raise APIError(409, 40902, str(exc))
    if result is None:
        raise APIError(404, 40404, '采集任务不存在')
    return dict(code=0, message='ok', data=result)


@app.post('/api/collections/{task_id}/execute', dependencies=[Depends(authorize)])
def collection_execute(task_id: str, request: Request):
    from collection_tasks import CollectionError
    try:
        result = request.app.state.collections.execute(task_id)
    except CollectionError as exc:
        raise APIError(409, 40902, str(exc))
    if result is None:
        raise APIError(404, 40404, '采集任务不存在')
    return dict(code=0, message='queued', data=result)


class BulkIdsInput(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class ResourceIdsInput(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=1000)


class ImportScriptInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field('', max_length=500)
    code: str = Field(min_length=1, max_length=100000)


class DownloadSignerInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    code: str = Field('', max_length=100000)
    config: dict = Field(default_factory=dict)
    image_base_url: str = Field('', max_length=8192)
    video_base_url: str = Field('', max_length=8192)
    format_code: str = Field('', max_length=100000)


class DownloadSignerTestInput(DownloadSignerInput):
    url: str = Field(min_length=1, max_length=8192)
    resource_type: str = Field('playlist', min_length=1, max_length=50)


class DownloadFormatterTestInput(DownloadSignerInput):
    raw: object


@app.post('/api/collections/bulk-execute', dependencies=[Depends(authorize)])
def collection_bulk_execute(body: BulkIdsInput, request: Request):
    return dict(code=0, message='queued', data=request.app.state.collections.execute_many(body.ids))


@app.post('/api/collections/bulk-delete', dependencies=[Depends(authorize)])
def collection_bulk_delete(body: BulkIdsInput, request: Request):
    result = request.app.state.collections.delete_many(body.ids)
    return dict(code=0, message='deleted', data=result)


@app.delete('/api/collections/{task_id}', dependencies=[Depends(authorize)])
def collection_delete(task_id: str, request: Request):
    from collection_tasks import CollectionError
    try:
        deleted = request.app.state.collections.delete(task_id)
    except CollectionError as exc:
        raise APIError(409, 40902, str(exc))
    if not deleted:
        raise APIError(404, 40404, '采集任务不存在')
    return dict(code=0, message='deleted', data=dict(id=task_id, deleted=True))


@app.get('/api/collection-items', dependencies=[Depends(authorize)])
def collection_item_list(request: Request, response: Response,
                         page: int = Query(1, ge=1),
                         page_size: int = Query(20, ge=1, le=100),
                         title: str | None = Query(None, max_length=8192),
                         source: str | None = Query(None), status: str | None = Query(None),
                         collection_id: str | None = Query(None, max_length=32),
                         include_sync_state: bool = Query(True, alias='includeSyncState')):
    if source and not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', source):
        raise APIError(400, 40001, '来源筛选参数无效')
    if status and status not in {'pending', 'waiting_collector', 'queued', 'downloading', 'completed', 'failed'}:
        raise APIError(400, 40001, '资源状态筛选参数无效')
    from collection_tasks import CollectionError
    started = time.perf_counter()
    try:
        result = request.app.state.collections.list_items(
            page, page_size, title, source, status, collection_id)
    except CollectionError as exc:
        raise APIError(400, 40001, str(exc))
    listed = time.perf_counter()
    if include_sync_state:
        result['node_queue'] = request.app.state.resource_sync.collection_item_node_state(
            result['items'])
    else:
        for item in result['items']:
            item['synced_nodes'] = []
        result['node_queue'] = dict(queued=0, processing=0, succeeded=0, failed=0)
    synced = time.perf_counter()
    request.app.state.resource_preview.decorate_list(result['items'])
    decorated = time.perf_counter()
    response.headers['Server-Timing'] = (
        f'items;dur={(listed - started) * 1000:.1f}, '
        f'sync;dur={(synced - listed) * 1000:.1f}, '
        f'preview;dur={(decorated - synced) * 1000:.1f}, '
        f'total;dur={(decorated - started) * 1000:.1f}')
    return dict(code=0, message='ok', data=result)


@app.get('/api/collection-items/{task_id}/playback', dependencies=[Depends(authorize)])
def collection_item_playback(task_id: str, request: Request):
    task = request.app.state.downloads.get(task_id)
    if not task:
        raise APIError(404, 40402, '资源不存在')
    if not task.get('m3u8_url'):
        raise APIError(409, 40902, '来源尚未提供播放地址，请先采集详情')
    return dict(code=0, message='ok', data=request.app.state.resource_preview.playback(task))


@app.get('/api/import-scripts', dependencies=[Depends(authorize)])
def import_script_list(request: Request):
    return dict(code=0, message='ok', data=request.app.state.import_scripts.list())


@app.post('/api/import-scripts', dependencies=[Depends(authorize)])
def import_script_create(body: ImportScriptInput, request: Request):
    from import_scripts import ImportScriptError
    try:
        result = request.app.state.import_scripts.create(body.name, body.description, body.code)
    except ImportScriptError as exc:
        raise APIError(409, 40907, str(exc))
    return dict(code=0, message='created', data=result)


@app.put('/api/import-scripts/{script_id}', dependencies=[Depends(authorize)])
def import_script_update(script_id: str, body: ImportScriptInput, request: Request):
    from import_scripts import ImportScriptError
    if not re.fullmatch(r'[a-f0-9]{32}', script_id):
        raise APIError(404, 40407, '格式化脚本不存在')
    try:
        result = request.app.state.import_scripts.update(
            script_id, body.name, body.description, body.code)
    except ImportScriptError as exc:
        raise APIError(409, 40907, str(exc))
    if result is None:
        raise APIError(404, 40407, '格式化脚本不存在')
    return dict(code=0, message='ok', data=result)


@app.delete('/api/import-scripts/{script_id}', dependencies=[Depends(authorize)])
def import_script_delete(script_id: str, request: Request):
    if not re.fullmatch(r'[a-f0-9]{32}', script_id):
        raise APIError(404, 40407, '格式化脚本不存在')
    if not request.app.state.import_scripts.delete(script_id):
        raise APIError(404, 40407, '格式化脚本不存在')
    return dict(code=0, message='deleted', data=dict(id=script_id, deleted=True))


@app.get('/api/download-signers', dependencies=[Depends(authorize)])
def download_signer_list(request: Request):
    return dict(code=0, message='ok', data=request.app.state.download_signers.list())


@app.post('/api/download-signers/test', dependencies=[Depends(authorize)])
def download_signer_test(body: DownloadSignerTestInput, request: Request):
    from download_signers import SignerError
    try:
        result = request.app.state.download_signers.test(
            body.name, body.source, body.enabled, body.code, body.config,
            body.url, body.resource_type, body.image_base_url, body.video_base_url, body.format_code)
    except SignerError as exc:
        raise APIError(400, 40008, str(exc))
    return dict(code=0, message='ok', data=result)


@app.post('/api/download-signers/format-test', dependencies=[Depends(authorize)])
def download_formatter_test(body: DownloadFormatterTestInput, request: Request):
    from download_signers import SignerError
    manager = request.app.state.download_signers
    try:
        name, source, enabled, code, config = manager.validate(
            body.name, body.source, body.enabled, body.code, body.config)
        image_base, video_base, format_code = manager.validate_options(
            body.image_base_url, body.video_base_url, body.format_code)
        definition = dict(source=source, enabled=enabled, code=code, config=config,
                          format_code=format_code, image_base_url=image_base, video_base_url=video_base)
        items = manager.format_items(body.raw, source, definition=definition)
    except SignerError as exc:
        raise APIError(400, 40008, str(exc)) from None
    return dict(code=0, message='ok', data=dict(items=items, total=len(items)))


@app.post('/api/download-signers', dependencies=[Depends(authorize)])
def download_signer_create(body: DownloadSignerInput, request: Request):
    from download_signers import SignerError
    try:
        result = request.app.state.download_signers.create(
            body.name, body.source, body.enabled, body.code, body.config,
            body.image_base_url, body.video_base_url, body.format_code)
    except SignerError as exc:
        raise APIError(409, 40908, str(exc))
    return dict(code=0, message='created', data=result)


@app.put('/api/download-signers/{signer_id}', dependencies=[Depends(authorize)])
def download_signer_update(signer_id: str, body: DownloadSignerInput, request: Request):
    from download_signers import SignerError
    if not re.fullmatch(r'[a-f0-9]{32}', signer_id):
        raise APIError(404, 40408, '签名脚本不存在')
    try:
        result = request.app.state.download_signers.update(
            signer_id, body.name, body.source, body.enabled, body.code, body.config,
            body.image_base_url, body.video_base_url, body.format_code)
    except SignerError as exc:
        raise APIError(409, 40908, str(exc))
    if result is None:
        raise APIError(404, 40408, '签名脚本不存在')
    return dict(code=0, message='ok', data=result)


@app.delete('/api/download-signers/{signer_id}', dependencies=[Depends(authorize)])
def download_signer_delete(signer_id: str, request: Request):
    if not re.fullmatch(r'[a-f0-9]{32}', signer_id):
        raise APIError(404, 40408, '签名脚本不存在')
    if not request.app.state.download_signers.delete(signer_id):
        raise APIError(404, 40408, '签名脚本不存在')
    return dict(code=0, message='deleted', data=dict(id=signer_id, deleted=True))


@app.post('/api/collection-items/import/validate', dependencies=[Depends(authorize)])
def collection_item_import_validate(request: Request, body: object = Body(...),
                                    source: str | None = Query(None, max_length=100),
                                    format: bool = Query(True)):
    items = prepare_import_items(request, body, source, format)
    result = request.app.state.collections.validate_import_items(items)
    result['items'] = items
    return dict(code=0, message='ok', data=result)


def prepare_import_items(request, raw, source=None, apply_format=True):
    from download_signers import SignerError
    try:
        return request.app.state.download_signers.format_items(raw, source, apply_format)
    except SignerError as exc:
        raise APIError(400, 40008, str(exc)) from None


@app.post('/api/collection-items/import', dependencies=[Depends(authorize)])
def collection_item_import(request: Request, body: object = Body(...),
                           source: str | None = Query(None, max_length=100),
                           format: bool = Query(True)):
    items = prepare_import_items(request, body, source, format)
    return dict(code=0, message='ok', data=request.app.state.collections.import_items(items))


@app.post('/api/collection-items/export', dependencies=[Depends(authorize)])
def collection_item_export(body: ResourceIdsInput, request: Request):
    result = request.app.state.collections.export_items(body.ids)
    if result['missing']:
        raise APIError(404, 40405, '部分资源不存在，请刷新列表后重试')
    content = __import__('json').dumps(result['items'], ensure_ascii=False, indent=2)
    filename = time.strftime('resources-%Y%m%d-%H%M%S.json')
    return Response(content, media_type='application/json; charset=utf-8', headers={
        'Content-Disposition': f'attachment; filename="{filename}"',
        'Cache-Control': 'no-store',
    })


@app.post('/api/collection-items/{item_id}/refresh-detail', dependencies=[Depends(authorize)])
def collection_item_refresh_detail(item_id: str, request: Request):
    if not re.fullmatch(r'[a-f0-9]{32}', item_id):
        raise APIError(404, 40405, '采集资源不存在')
    result = request.app.state.collections.refresh_details([item_id], resolve_detail)
    if result['failed'] and result['results'][0].get('message') == '资源不存在':
        raise APIError(404, 40405, '采集资源不存在')
    return dict(code=0, message='ok', data=result)


@app.post('/api/collection-items/bulk-refresh-detail', dependencies=[Depends(authorize)])
def collection_item_bulk_refresh_detail(body: BulkIdsInput, request: Request):
    return dict(code=0, message='ok',
                data=request.app.state.collections.refresh_details(body.ids, resolve_detail))


@app.post('/api/collection-items/{item_id}/download', dependencies=[Depends(authorize)])
def collection_item_download(item_id: str, request: Request):
    from collection_tasks import CollectionError
    try:
        result = request.app.state.collections.schedule_item(item_id)
    except CollectionError as exc:
        raise APIError(409, 40902, str(exc)) from None
    if result is None:
        raise APIError(404, 40405, '采集资源不存在')
    return dict(code=0, message='queued', data=result)


@app.post('/api/collection-items/bulk-download', dependencies=[Depends(authorize)])
def collection_item_bulk_download(body: BulkIdsInput, request: Request):
    return dict(code=0, message='queued', data=request.app.state.collections.schedule_items(body.ids))


@app.post('/api/collection-items/bulk-delete', dependencies=[Depends(authorize)])
def collection_item_bulk_delete(body: BulkIdsInput, request: Request):
    result = request.app.state.downloads.delete_many(body.ids)
    return dict(code=0, message='deleted', data=result)


@app.post('/api/collection-items/download-all', dependencies=[Depends(authorize)])
def collection_item_download_all(request: Request):
    result = request.app.state.collections.enqueue_all_pending()
    return dict(code=0, message='queued', data=result)


class SchedulerSettingsInput(BaseModel):
    enabled: bool
    concurrency: int = Field(1, ge=1, le=10)
    segment_concurrency: int = Field(3, ge=1, le=16)


class PublicBaseUrlInput(BaseModel):
    url: str = Field(min_length=1, max_length=2048,
                     description='公开访问根地址，例如 http://localhost:8000')


class PlayerProxySettingsInput(BaseModel):
    enabled: bool = False


def scheduler_settings_data(request: Request):
    result = request.app.state.collections.settings()
    speeds = request.app.state.r2_sync.upload_speeds()
    result['upload_speed'] = speeds['total']
    result['video_upload_speed'] = speeds['video']
    result['other_upload_speed'] = speeds['other']
    return result


@app.get('/api/settings/scheduler', dependencies=[Depends(authorize)])
def scheduler_settings(request: Request):
    result = scheduler_settings_data(request)
    return dict(code=0, message='ok', data=result)


@app.get('/api/settings/all', dependencies=[Depends(authorize)])
def all_settings(request: Request):
    """Load the management settings dialog with one HTTP request."""
    return dict(code=0, message='ok', data=dict(
        publicUrl=request.app.state.downloads.public_url_settings(),
        scheduler=scheduler_settings_data(request),
        ffmpeg=request.app.state.uploads.ffmpeg_settings(),
        compression=request.app.state.uploads.compression_settings(),
        imageCompression=request.app.state.uploads.image_compression_settings(),
        chunkCleanup=request.app.state.uploads.chunk_cleanup_settings(),
        database=request.app.state.downloads.database_settings(DATABASE_BACKUP_PATH),
        r2=request.app.state.r2_sync.settings()))


@app.get('/api/database/settings', dependencies=[Depends(authorize)])
def database_settings(request: Request):
    return dict(code=0, message='ok', data=
                request.app.state.downloads.database_settings(DATABASE_BACKUP_PATH))


@app.post('/api/database/backup', dependencies=[Depends(authorize)])
def database_backup(request: Request):
    from download_tasks import DownloadError
    try:
        result = request.app.state.downloads.backup_database(DATABASE_BACKUP_PATH)
    except DownloadError as exc:
        raise APIError(409, 40910, str(exc)) from None
    return dict(code=0, message='数据库备份完成', data=result)


@app.put('/api/settings/scheduler', dependencies=[Depends(authorize)])
def scheduler_settings_update(body: SchedulerSettingsInput, request: Request):
    result = request.app.state.collections.update_settings(
        body.enabled, body.concurrency, body.segment_concurrency)
    speeds = request.app.state.r2_sync.upload_speeds()
    result['upload_speed'] = speeds['total']
    result['video_upload_speed'] = speeds['video']
    result['other_upload_speed'] = speeds['other']
    return dict(code=0, message='ok', data=result)


@app.get('/api/settings/public-url', dependencies=[Depends(authorize)])
def public_url_settings(request: Request):
    return dict(code=0, message='ok', data=request.app.state.downloads.public_url_settings())


@app.put('/api/settings/public-url', dependencies=[Depends(authorize)])
def public_url_settings_update(body: PublicBaseUrlInput, request: Request):
    try:
        result = request.app.state.downloads.set_public_base_url(body.url)
    except ValueError as exc:
        raise APIError(400, 40010, str(exc)) from None
    return dict(code=0, message='ok', data=result)


@app.get('/api/player-proxy/settings', dependencies=[Depends(authorize)])
def player_proxy_settings(request: Request):
    return dict(code=0, message='ok', data=request.app.state.player_proxy.settings())


@app.put('/api/player-proxy/settings', dependencies=[Depends(authorize)])
def player_proxy_settings_update(body: PlayerProxySettingsInput, request: Request):
    result = request.app.state.player_proxy.set_enabled(body.enabled)
    return dict(code=0, message='ok', data=result)


@app.api_route('/api/player/proxy', methods=['GET', 'HEAD'])
async def player_proxy(request: Request,
                       url: str = Query(min_length=1, max_length=8192),
                       referer: str | None = Query(None, max_length=8192)):
    """Public only while the global player proxy switch is enabled."""
    from player_proxy import PlayerProxyError
    range_header = request.headers.get('range')
    try:
        remote = await run_in_threadpool(
            request.app.state.player_proxy.open,
            url, request.method, range_header, referer)
    except PlayerProxyError as exc:
        error_codes = {400: 40040, 403: 40301, 416: 41601, 502: 50210}
        raise APIError(exc.status_code, error_codes.get(exc.status_code, 50210),
                       str(exc)) from None
    manager = request.app.state.player_proxy
    return media_proxy_response(manager, remote, check_enabled=True)


@app.api_route('/api/source-media', methods=['GET', 'HEAD'])
async def source_media(request: Request,
                       source: str = Query(min_length=1, max_length=100),
                       url: str = Query(min_length=1, max_length=8192),
                       resource_type: str = Query(max_length=50),
                       token: str = Query(max_length=200)):
    """Resource-specific media grants; all child requests are signed at request time."""
    from player_proxy import PlayerProxyError
    try:
        remote = await run_in_threadpool(request.app.state.resource_preview.open,
            source, url, resource_type, token, request.method, request.headers.get('range'))
    except PlayerProxyError as exc:
        raise APIError(exc.status_code, 40101 if exc.status_code == 401 else 50210, str(exc)) from None
    return media_proxy_response(request.app.state.player_proxy, remote)


def media_proxy_response(manager, remote, check_enabled=False):
    def available():
        return not check_enabled or manager.available(remote['generation'])

    if remote['kind'] == 'body':
        if not available():
            raise APIError(403, 40301, '播放器代理未开启')
        return Response(remote['body'], status_code=remote['status'],
                        headers=remote['headers'], media_type=remote['media_type'])
    if remote['kind'] == 'head':
        try:
            if not available():
                raise APIError(403, 40301, '播放器代理未开启')
            return Response(status_code=remote['status'], headers=remote['headers'],
                            media_type=remote['media_type'])
        finally:
            manager.close_result(remote)

    def stream():
        try:
            for chunk in remote['response'].iter_content(256 * 1024):
                if not available():
                    break
                if chunk:
                    yield chunk
        finally:
            manager.close_result(remote)

    if not available():
        manager.close_result(remote)
        raise APIError(403, 40301, '播放器代理未开启')
    return StreamingResponse(stream(), status_code=remote['status'],
                             headers=remote['headers'], media_type=remote['media_type'])


class ResourceSyncSiteInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = False
    endpoint_url: str = Field(min_length=1, max_length=2048)
    request_headers: dict[str, str] | None = None
    resolve_media: bool = False
    batch_size: int = Field(50, ge=1, le=100)
    timeout: int = Field(30, ge=5, le=120)


class ResourceSyncExecuteInput(BaseModel):
    site_id: str = Field(min_length=32, max_length=32)
    ids: list[str] = Field(default_factory=list, max_length=10000)
    all: bool = False
    force: bool = False


class ResourceSyncRecordRetryInput(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=10000)
    all: bool = False


class ResourceSyncRecordDeleteInput(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=10000)


@app.get('/api/resource-sync/sites', dependencies=[Depends(authorize)])
def resource_sync_sites(request: Request):
    return dict(code=0, message='ok', data=request.app.state.resource_sync.list_sites())


@app.post('/api/resource-sync/sites', dependencies=[Depends(authorize)])
def resource_sync_site_create(body: ResourceSyncSiteInput, request: Request):
    from resource_sync import SyncError
    try:
        result = request.app.state.resource_sync.create_site(
            body.name, body.enabled, body.endpoint_url, body.batch_size, body.timeout,
            body.resolve_media, body.request_headers)
    except SyncError as exc:
        raise APIError(400, 40006, str(exc))
    return dict(code=0, message='created', data=result)


@app.put('/api/resource-sync/sites/{site_id}', dependencies=[Depends(authorize)])
def resource_sync_site_update(site_id: str, body: ResourceSyncSiteInput, request: Request):
    from resource_sync import SyncError
    if not re.fullmatch(r'[a-f0-9]{32}', site_id):
        raise APIError(404, 40406, '第三方网站不存在')
    try:
        result = request.app.state.resource_sync.update_site(
            site_id, body.name, body.enabled, body.endpoint_url, body.batch_size, body.timeout,
            body.resolve_media, body.request_headers)
    except SyncError as exc:
        raise APIError(400, 40006, str(exc))
    return dict(code=0, message='ok', data=result)


@app.delete('/api/resource-sync/sites/{site_id}', dependencies=[Depends(authorize)])
def resource_sync_site_delete(site_id: str, request: Request):
    if not re.fullmatch(r'[a-f0-9]{32}', site_id):
        raise APIError(404, 40406, '第三方网站不存在')
    if not request.app.state.resource_sync.delete_site(site_id):
        raise APIError(404, 40406, '第三方网站不存在')
    return dict(code=0, message='deleted', data=dict(id=site_id, deleted=True))


@app.get('/api/resource-sync/runs', dependencies=[Depends(authorize)])
def resource_sync_runs(request: Request, limit: int = Query(20, ge=1, le=100),
                       site_id: str | None = Query(None, min_length=32, max_length=32)):
    return dict(code=0, message='ok', data=request.app.state.resource_sync.list_runs(limit, site_id))


@app.post('/api/resource-sync/execute', dependencies=[Depends(authorize)])
def resource_sync_execute(body: ResourceSyncExecuteInput, request: Request):
    from resource_sync import SyncError
    try:
        result = request.app.state.resource_sync.sync(
            body.site_id, body.ids, body.all, force=body.force)
    except SyncError as exc:
        raise APIError(409, 40906, str(exc))
    return dict(code=0, message='ok', data=result)


@app.post('/api/resource-sync/runs/{run_id}/retry', dependencies=[Depends(authorize)])
def resource_sync_retry(run_id: str, request: Request):
    from resource_sync import SyncError
    if not re.fullmatch(r'[a-f0-9]{32}', run_id):
        raise APIError(404, 40406, '同步记录不存在')
    try:
        result = request.app.state.resource_sync.retry(run_id)
    except SyncError as exc:
        raise APIError(409, 40906, str(exc))
    return dict(code=0, message='ok', data=result)


@app.get('/api/resource-sync/records', dependencies=[Depends(authorize)])
def resource_sync_records(request: Request, page: int = Query(1, ge=1),
                          page_size: int = Query(20, ge=1, le=100),
                          keyword: str | None = Query(None, max_length=200),
                          status: str | None = Query(None, max_length=20),
                          site_id: str | None = Query(None, min_length=32, max_length=32)):
    from resource_sync import SyncError
    try:
        result = request.app.state.resource_sync.list_records(
            page, page_size, keyword, status, site_id)
    except SyncError as exc:
        raise APIError(400, 40009, str(exc))
    return dict(code=0, message='ok', data=result)


@app.post('/api/resource-sync/records/retry', dependencies=[Depends(authorize)])
def resource_sync_record_retry(body: ResourceSyncRecordRetryInput, request: Request):
    from resource_sync import SyncError
    if not body.all and not body.ids:
        raise APIError(400, 40009, '请选择要重试的失败记录')
    try:
        result = request.app.state.resource_sync.retry_records(body.ids, body.all)
    except SyncError as exc:
        raise APIError(409, 40909, str(exc))
    return dict(code=0, message='ok', data=result)


@app.post('/api/resource-sync/records/delete', dependencies=[Depends(authorize)])
def resource_sync_record_delete(body: ResourceSyncRecordDeleteInput, request: Request):
    from resource_sync import SyncError
    try:
        result = request.app.state.resource_sync.delete_records(body.ids)
    except SyncError as exc:
        raise APIError(400, 40009, str(exc))
    return dict(code=0, message='deleted', data=result)


class MergeUploadInput(BaseModel):
    identifier: str = Field(min_length=1, max_length=128)
    file_name: str = Field(alias='fileName', min_length=1, max_length=255)
    r2_sync_enabled: bool | None = Field(default=None, alias='r2SyncEnabled')


class UploadIdsInput(BaseModel):
    identifiers: list[str] = Field(min_length=1, max_length=100)


class FfmpegSettingsInput(BaseModel):
    concurrency: int = Field(ge=0, le=64)


class FfmpegCompressionSettingsInput(BaseModel):
    enabled: bool
    compatibility_only: bool = Field(default=True, alias='compatibilityOnly')
    generate_hover_preview: bool = Field(default=True, alias='generateHoverPreview')
    generate_vtt_preview: bool = Field(default=True, alias='generateVttPreview')
    min_file_size_mb: float = Field(alias='minFileSizeMb', ge=1, le=102400)
    min_duration_minutes: float = Field(alias='minDurationMinutes', ge=1, le=1440)
    max_video_bitrate_mbps: float = Field(alias='maxVideoBitrateMbps', ge=0.1, le=100)
    max_width: int = Field(alias='maxWidth', ge=320, le=7680)
    max_height: int = Field(alias='maxHeight', ge=240, le=4320)
    crf: int = Field(ge=0, le=51)
    preset: str = Field(min_length=4, max_length=16)
    audio_bitrate_kbps: int = Field(alias='audioBitrateKbps', ge=32, le=512)
    hls_segment_seconds: int = Field(alias='hlsSegmentSeconds', ge=1, le=30)


class ImageCompressionSettingsInput(BaseModel):
    enabled: bool
    min_file_size_mb: float = Field(alias='minFileSizeMb', ge=0.1, le=102400)
    quality: int = Field(ge=1, le=100)


class ChunkCleanupSettingsInput(BaseModel):
    enabled: bool = True
    retention_days: int = Field(default=7, alias='retentionDays', ge=1, le=3650)


class R2SettingsInput(BaseModel):
    enabled: bool = False
    endpoint_url: str = Field(alias='endpointUrl', max_length=2048)
    access_key_id: str = Field(alias='accessKeyId', max_length=512)
    secret_access_key: str | None = Field(default=None, alias='secretAccessKey', max_length=2048)
    bucket: str = Field(max_length=255)
    public_base_url: str = Field(default='', alias='publicBaseUrl', max_length=2048)
    path_prefix: str = Field(default='', alias='pathPrefix', max_length=512)
    video_concurrency: int | None = Field(default=None, alias='videoConcurrency', ge=0, le=16)
    video_file_concurrency: int | None = Field(
        default=None, alias='videoFileConcurrency', ge=1, le=16)
    other_concurrency: int | None = Field(default=None, alias='otherConcurrency', ge=0, le=16)
    other_file_concurrency: int | None = Field(
        default=None, alias='otherFileConcurrency', ge=1, le=16)
    # Compatibility for a management page cached before the queue split.
    concurrency: int | None = Field(default=None, ge=0, le=16)
    file_concurrency: int | None = Field(default=None, alias='fileConcurrency', ge=1, le=16)
    auto_upload: bool = Field(default=False, alias='autoUpload')
    auto_collection: bool = Field(default=False, alias='autoCollection')
    cleanup_enabled: bool = Field(default=False, alias='cleanupEnabled')
    retention_days: int = Field(default=7, alias='retentionDays', le=3650)


class R2TaskIdsInput(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)


@app.get('/api/r2/settings', dependencies=[Depends(authorize)])
def r2_settings(request: Request):
    return dict(code=0, message='ok', data=request.app.state.r2_sync.settings())


@app.put('/api/r2/settings', dependencies=[Depends(authorize)])
def update_r2_settings(body: R2SettingsInput, request: Request):
    from r2_sync import R2SyncError
    try:
        result = request.app.state.r2_sync.save_settings(body.model_dump())
    except R2SyncError as exc:
        raise APIError(400, 40030, str(exc))
    return dict(code=0, message='saved', data=result)


@app.post('/api/r2/settings/test', dependencies=[Depends(authorize)])
def test_r2_settings(body: R2SettingsInput, request: Request):
    from r2_sync import R2SyncError
    try:
        result = request.app.state.r2_sync.test_settings(body.model_dump())
    except R2SyncError as exc:
        raise APIError(400, 40030, str(exc))
    return dict(code=0, message='connected', data=result)


@app.get('/api/r2/tasks', dependencies=[Depends(authorize)])
def r2_task_list(request: Request, page: int = Query(1, ge=1),
                 page_size: int = Query(20, ge=1, le=100),
                 keyword: str | None = Query(None, max_length=255),
                 status: str | None = Query(None),
                 resource_type: str | None = Query(None),
                 sync_group: str | None = Query(None)):
    if status and status not in {'queued', 'running', 'completed', 'failed'}:
        raise APIError(400, 40001, 'R2 同步状态筛选参数无效')
    if resource_type and resource_type not in {'collection', 'upload'}:
        raise APIError(400, 40001, 'R2资源来源筛选参数无效')
    if sync_group and sync_group not in {'video', 'other'}:
        raise APIError(400, 40001, 'R2同步分组筛选参数无效')
    result = request.app.state.r2_sync.list_tasks(
        page, page_size, keyword.strip() if keyword else None,
        status, resource_type, sync_group)
    return dict(code=0, message='ok', data=result)


@app.post('/api/r2/tasks/retry', dependencies=[Depends(authorize)])
def retry_r2_tasks(body: R2TaskIdsInput, request: Request):
    result = request.app.state.r2_sync.retry(body.ids)
    return dict(code=0, message='queued', data=result)


@app.post('/api/r2/tasks/delete', dependencies=[Depends(authorize)])
def delete_r2_tasks(body: R2TaskIdsInput, request: Request):
    result = request.app.state.r2_sync.delete_tasks(body.ids)
    return dict(code=0, message='deleted', data=result)


@app.post('/api/r2/resources/sync', dependencies=[Depends(authorize)])
def sync_resources_to_r2(body: R2TaskIdsInput, request: Request):
    from r2_sync import R2SyncError
    try:
        result = request.app.state.r2_sync.enqueue_download_ids(body.ids)
    except R2SyncError as exc:
        raise APIError(400, 40030, str(exc))
    return dict(code=0, message='queued', data=result)


@app.post('/api/r2/uploads/sync', dependencies=[Depends(authorize)])
def sync_uploads_to_r2(body: R2TaskIdsInput, request: Request):
    from r2_sync import R2SyncError
    try:
        result = request.app.state.r2_sync.enqueue_upload_ids(body.ids)
    except R2SyncError as exc:
        raise APIError(400, 40030, str(exc))
    return dict(code=0, message='queued', data=result)


@app.post('/api/r2/uploads/cleanup-local', dependencies=[Depends(authorize)])
def cleanup_uploaded_local_files(body: R2TaskIdsInput, request: Request):
    result = request.app.state.r2_sync.cleanup_uploaded_local_files(body.ids)
    return dict(code=0, message='cleaned', data=result)


@app.post('/upload/file', dependencies=[Depends(authorize)])
def upload_file(request: Request, file: UploadFile = File(...),
                r2_sync_enabled: bool = Form(False, alias='r2SyncEnabled')):
    if r2_sync_enabled and not request.app.state.r2_sync.settings()['enabled']:
        raise APIError(400, 40030, '请先配置并启用R2')
    result = request.app.state.uploads.upload_file(file, r2_sync_enabled)
    return dict(code=0, message='ok', data=result)


@app.get('/upload/chunk', dependencies=[Depends(authorize)])
def upload_chunk_status(request: Request,
                        identifier: str = Query(..., min_length=1, max_length=128),
                        include_chunks: bool = Query(True, alias='includeChunks')):
    result = request.app.state.uploads.check_chunks(identifier, include_chunks)
    return dict(code=0, message='ok', data=result)


@app.get('/upload/list', dependencies=[Depends(authorize)])
def upload_list(request: Request, page: int = Query(1, ge=1),
                page_size: int = Query(20, ge=1, le=100),
                keyword: str | None = Query(None, max_length=2048),
                file_type: str | None = Query(None), status: str | None = Query(None)):
    if file_type and file_type not in {'video', 'image', 'other'}:
        raise APIError(400, 40001, '上传文件类型筛选参数无效')
    allowed_status = {'uploading', 'merging', 'queued', 'probing', 'compressing',
                      'segmenting', 'generating_cover', 'generating_preview',
                      'generating_vtt', 'completed', 'failed', 'cancelled'}
    if status and status not in allowed_status:
        raise APIError(400, 40001, '上传状态筛选参数无效')
    result = request.app.state.uploads.list_uploads(
        page, page_size, keyword.strip() if keyword else None, file_type, status)
    return dict(code=0, message='ok', data=result)


@app.get('/api/ffmpeg/tasks', dependencies=[Depends(authorize)])
def ffmpeg_task_list(request: Request, page: int = Query(1, ge=1),
                     page_size: int = Query(20, ge=1, le=100),
                     keyword: str | None = Query(None, max_length=255),
                     status: str | None = Query(None)):
    allowed = {'queued', 'running', 'probing', 'compressing', 'segmenting',
               'generating_cover', 'generating_preview', 'generating_vtt',
               'completed', 'failed', 'cancelled'}
    if status and status not in allowed:
        raise APIError(400, 40001, 'FFmpeg 任务状态筛选参数无效')
    result = request.app.state.uploads.list_ffmpeg_tasks(
        page, page_size, keyword.strip() if keyword else None, status)
    return dict(code=0, message='ok', data=result)


@app.get('/api/ffmpeg/settings', dependencies=[Depends(authorize)])
def ffmpeg_settings(request: Request):
    return dict(code=0, message='ok', data=request.app.state.uploads.ffmpeg_settings())


@app.put('/api/ffmpeg/settings', dependencies=[Depends(authorize)])
def update_ffmpeg_settings(body: FfmpegSettingsInput, request: Request):
    result = request.app.state.uploads.set_ffmpeg_concurrency(body.concurrency)
    return dict(code=0, message='saved', data=result)


@app.get('/api/ffmpeg/compression-settings', dependencies=[Depends(authorize)])
def ffmpeg_compression_settings(request: Request):
    return dict(
        code=0, message='ok', data=request.app.state.uploads.compression_settings())


@app.put('/api/ffmpeg/compression-settings', dependencies=[Depends(authorize)])
def update_ffmpeg_compression_settings(
        body: FfmpegCompressionSettingsInput, request: Request):
    result = request.app.state.uploads.set_compression_settings({
        'enabled': body.enabled,
        'compatibilityOnly': body.compatibility_only,
        'generateHoverPreview': body.generate_hover_preview,
        'generateVttPreview': body.generate_vtt_preview,
        'minFileSizeMb': body.min_file_size_mb,
        'minDurationMinutes': body.min_duration_minutes,
        'maxVideoBitrateMbps': body.max_video_bitrate_mbps,
        'maxWidth': body.max_width, 'maxHeight': body.max_height,
        'crf': body.crf, 'preset': body.preset,
        'audioBitrateKbps': body.audio_bitrate_kbps,
        'hlsSegmentSeconds': body.hls_segment_seconds,
    })
    return dict(code=0, message='saved', data=result)


@app.post('/api/ffmpeg/compression-settings/reset', dependencies=[Depends(authorize)])
def reset_ffmpeg_compression_settings(request: Request):
    result = request.app.state.uploads.reset_compression_settings()
    return dict(code=0, message='reset', data=result)


@app.get('/api/image/compression-settings', dependencies=[Depends(authorize)])
def image_compression_settings(request: Request):
    return dict(
        code=0, message='ok',
        data=request.app.state.uploads.image_compression_settings())


@app.put('/api/image/compression-settings', dependencies=[Depends(authorize)])
def update_image_compression_settings(
        body: ImageCompressionSettingsInput, request: Request):
    result = request.app.state.uploads.set_image_compression_settings({
        'enabled': body.enabled,
        'minFileSizeMb': body.min_file_size_mb,
        'quality': body.quality,
    })
    return dict(code=0, message='saved', data=result)


@app.get('/api/upload/chunk-cleanup/settings', dependencies=[Depends(authorize)])
def chunk_cleanup_settings(request: Request):
    return dict(code=0, message='ok', data=request.app.state.uploads.chunk_cleanup_settings())


@app.put('/api/upload/chunk-cleanup/settings', dependencies=[Depends(authorize)])
def update_chunk_cleanup_settings(body: ChunkCleanupSettingsInput, request: Request):
    result = request.app.state.uploads.set_chunk_cleanup_settings({
        'enabled': body.enabled, 'retentionDays': body.retention_days,
    })
    return dict(code=0, message='saved', data=result)


@app.post('/api/upload/chunk-cleanup/run', dependencies=[Depends(authorize)])
def run_chunk_cleanup(request: Request):
    result = request.app.state.uploads.cleanup_expired_chunks()
    return dict(code=0, message='cleaned', data=result)


@app.post('/api/ffmpeg/tasks/retry', dependencies=[Depends(authorize)])
def retry_ffmpeg_tasks(body: UploadIdsInput, request: Request):
    result = request.app.state.uploads.retry_ffmpeg_tasks(body.identifiers)
    return dict(code=0, message='ok', data=result)


@app.post('/api/ffmpeg/tasks/cancel', dependencies=[Depends(authorize)])
def cancel_ffmpeg_tasks(body: UploadIdsInput, request: Request):
    result = request.app.state.uploads.cancel_ffmpeg_tasks(body.identifiers)
    return dict(code=0, message='ok', data=result)


@app.delete('/api/ffmpeg/tasks/{identifier}', dependencies=[Depends(authorize)])
def delete_ffmpeg_task(identifier: str, request: Request):
    if not request.app.state.uploads.delete_ffmpeg_task(identifier):
        raise APIError(409, 40920, '只能删除已完成、失败或已取消的 FFmpeg 任务记录')
    return dict(code=0, message='deleted', data={'identifier': identifier})


@app.post('/upload/chunk', dependencies=[Depends(authorize)])
def upload_chunk(request: Request,
                 identifier: str = Form(..., min_length=1, max_length=128),
                 chunk_number: int = Form(..., alias='chunkNumber', ge=1),
                 total_chunks: int = Form(..., alias='totalChunks', ge=1),
                 r2_sync_enabled: bool = Form(False, alias='r2SyncEnabled'),
                 file: UploadFile = File(...)):
    if r2_sync_enabled and not request.app.state.r2_sync.settings()['enabled']:
        raise APIError(400, 40030, '请先配置并启用R2')
    result = request.app.state.uploads.upload_chunk(
        identifier, chunk_number, total_chunks, file, r2_sync_enabled)
    return dict(code=0, message='ok', data=result)


@app.post('/upload/merge', dependencies=[Depends(authorize)])
def merge_upload(request: Request, body: MergeUploadInput):
    if body.r2_sync_enabled and not request.app.state.r2_sync.settings()['enabled']:
        raise APIError(400, 40030, '请先配置并启用R2')
    result = request.app.state.uploads.merge(
        body.identifier, body.file_name, body.r2_sync_enabled)
    return dict(code=0, message='ok', data=result)


@app.post('/upload/deleteByPath', dependencies=[Depends(authorize)])
def delete_upload_paths(request: Request, paths: list[str]):
    if not paths or len(paths) > 100:
        raise APIError(400, 40001, '删除路径数量必须为 1—100 个')
    result = request.app.state.uploads.delete_paths(paths)
    return dict(code=0, message='ok', data=result)


@app.post('/upload/bulk-delete', dependencies=[Depends(authorize)])
def bulk_delete_uploads(body: UploadIdsInput, request: Request):
    result = request.app.state.uploads.delete_many(body.identifiers)
    return dict(code=0, message='deleted', data=result)


@app.delete('/upload/{identifier}', dependencies=[Depends(authorize)])
def delete_upload(identifier: str, request: Request):
    result = request.app.state.uploads.delete_identifier(identifier)
    if not result['deleted']:
        raise APIError(404, 40410, '上传资源不存在')
    return dict(code=0, message='deleted', data=result)


@app.get('/upload/files/{relative:path}', dependencies=[Depends(authorize)])
def uploaded_file(request: Request, relative: str):
    target = request.app.state.uploads.media_path(relative)
    if target is None:
        raise APIError(404, 40410, '上传文件不存在')
    media_types = {'.m3u8': 'application/vnd.apple.mpegurl', '.ts': 'video/mp2t',
                   '.vtt': 'text/vtt', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                   '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp',
                   '.mp4': 'video/mp4', '.webm': 'video/webm', '.pdf': 'application/pdf'}
    return FileResponse(target, media_type=media_types.get(target.suffix.lower()),
                        headers={'Cache-Control': 'private, max-age=3600'})


ADMIN_DIST = Path(__file__).parent / 'admin-ui' / 'dist'
if (ADMIN_DIST / 'assets').is_dir():
    app.mount('/admin/assets', StaticFiles(directory=ADMIN_DIST / 'assets'), name='admin-assets')


@app.get('/admin', include_in_schema=False)
@app.get('/admin/{path:path}', include_in_schema=False)
def admin_page(path: str = ''):
    index = ADMIN_DIST / 'index.html'
    if not index.is_file():
        raise HTTPException(status_code=503, detail='管理页面尚未构建，请先在 admin-ui 目录执行 npm run build')
    return FileResponse(index, media_type='text/html', headers={'Cache-Control': 'no-cache'})
