"""Run with: python -m uvicorn api:app --host 0.0.0.0 --port 8000."""
import hmac
import logging
import os
import re
import time
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from fastapi import Depends, FastAPI, Query, Security, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

from config import headers
from video_data import parse_detail, parse_list
import provider_123av

ALLOWED_HOSTS = {'jable.tv', 'www.jable.tv'}


def is_123av_host(hostname):
    hostname = (hostname or '').rstrip('.').lower()
    return hostname == '123av.com' or hostname.endswith('.123av.com')


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
API_KEY = os.getenv('JABLE_API_KEY', '')
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    from download_tasks import DownloadManager
    from collection_tasks import CollectionManager
    download_dir = os.getenv('DOWNLOAD_DIR', str(Path(__file__).parent / 'downloads'))
    database_path = os.getenv('DATABASE_PATH')
    app.state.downloads = DownloadManager(download_dir, start_enabled=False, database=database_path)
    def collect(url):
        source, final_url = fetch_page(url)
        return list_data(source, final_url, list_page(final_url))
    app.state.collections = CollectionManager(download_dir, app.state.downloads, collect)
    try:
        yield
    finally:
        await __import__('asyncio').to_thread(app.state.collections.close)
        await __import__('asyncio').to_thread(app.state.downloads.close)


app = FastAPI(title='JableTVDownload API', version='1.1.0', lifespan=lifespan)
key_header = APIKeyHeader(name='X-API-Key', auto_error=False)
browser_slot = threading.BoundedSemaphore(1)


def fetch_browser_page(url, detail=False):
    """Use the project's ordinary browser when HTTP receives a challenge."""
    from crawler import initWebDriver
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.support.ui import WebDriverWait

    url = validate_url(url, detail)
    if not browser_slot.acquire(timeout=TIMEOUT):
        raise APIError(503, 50301, '浏览器抓取繁忙，请稍后重试')
    driver = None
    try:
        driver = initWebDriver()
        driver.set_page_load_timeout(TIMEOUT)
        driver.get(url)
        def ready(browser):
            validate_url(browser.current_url, detail)
            source = browser.page_source
            return ((provider_123av.player_urls(source) if provider(url) == '123av'
                     else parse_detail(source, browser.current_url)['m3u8_url']) if detail
                    else list_data(source, browser.current_url, 1)['items'])
        WebDriverWait(driver, TIMEOUT).until(ready)
        final_url = validate_url(driver.current_url, detail)
        return driver.page_source, final_url
    except TimeoutException:
        raise APIError(502, 50203, '浏览器未能加载视频数据，来源可能仍要求验证或页面结构已变化')
    except WebDriverException:
        raise APIError(502, 50204, '浏览器抓取失败，请检查 Chromium 和 ChromeDriver 安装及服务日志')
    finally:
        try:
            if driver is not None:
                driver.quit()
        finally:
            browser_slot.release()


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


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
    return error_response(400, 40001, '参数错误，请检查页码、链接或请求 JSON 格式')


@app.exception_handler(HTTPException)
async def http_error_handler(request, exc):
    return error_response(exc.status_code, exc.status_code * 100, str(exc.detail))


@app.exception_handler(Exception)
async def unexpected_error_handler(request, exc):
    logger.error('API request failed', exc_info=(type(exc), exc, exc.__traceback__))
    return error_response(500, 50000, '服务内部错误')


def authorize(key: str | None = Security(key_header)):
    if API_KEY and (not key or not hmac.compare_digest(key.encode(), API_KEY.encode())):
        raise APIError(401, 40101, 'API Key 无效或缺失')


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
        valid = (parts.scheme == 'https' and (parts.hostname in ALLOWED_HOSTS or av123)
                 and parts.port in (None, 443) and not parts.username and not parts.password
                 and not re.search(r'[\s\\\x00-\x1f]', url)
                 and not parts.fragment)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        if av123:
            path_pattern = r'(?:/[^\s\\\x00-\x1f]*)?'
        else:
            path_pattern = (r'/search/[A-Za-z0-9_-]+/?' if list_search else
                            r'/videos/[A-Za-z0-9_-]+/?' if detail else
                            r'/(?:videos/[A-Za-z0-9_-]+|latest-updates(?:/[1-9][0-9]*)?)/?')
            valid = valid and not parts.query
        valid = valid and re.fullmatch(path_pattern, parts.path)
    except ValueError:
        valid = False
    if not valid:
        raise APIError(400, 40002, '仅支持 Jable 或 123AV 已支持的 HTTPS 页面地址及参数')
    if av123:
        return urlunsplit(('https', parts.hostname, parts.path or '/', parts.query, ''))
    path = parts.path.rstrip('/') + '/'
    return urlunsplit(('https', parts.hostname, path, urlencode(pairs), ''))


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
            for _ in range(6):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise requests.Timeout()
                with session.get(url, headers=headers, timeout=remaining, allow_redirects=False, stream=True) as response:
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
        if provider(url) == 'jable' and not urlsplit(url).path.startswith('/latest-updates/'):
            raise APIError(400, 40002, 'list 接口需要列表页地址')
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
    source, final_url = fetch_page(url, detail=True)
    result = detail_data(source, final_url)
    if provider(final_url) == '123av' and not result['m3u8_url']:
        players = provider_123av.player_urls(source)
        if players:
            result['m3u8_url'] = resolve_player(players[0], preview=result)
    if not result['m3u8_url']:
        raise APIError(502, 50202, '未解析到播放地址，页面结构可能变化或访问受限')
    return dict(code=0, message='ok', data=result)


class DownloadInput(BaseModel):
    url: str = Field(min_length=1, max_length=2048, description='Jable 或 123AV 视频详情链接')


class DownloadTask(BaseModel):
    task_id: str
    source: str
    video_id: str
    url: str
    status: str
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
    if status and status not in {'pending', 'queued', 'downloading', 'completed', 'failed', 'deleting'}:
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


@app.get('/api/download/{task_id}/files/{relative:path}', dependencies=[Depends(authorize_media)])
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


class CollectionInput(BaseModel):
    name: str | None = Field(None, max_length=80)
    url: str = Field(min_length=1, max_length=2048, description='Jable 或 123AV 列表页链接')
    batch_size: int = Field(1, ge=1, le=20, description='兼容字段；列表采集始终保存当前页全部数据')
    scheduled_enabled: bool = Field(False, description='每天北京时间 18:00 自动采集')


class CollectionTask(BaseModel):
    id: str
    name: str
    url: str
    batch_size: int
    scheduled_enabled: bool = False
    last_scheduled_date: str | None = None
    status: str
    last_total: int
    last_created: int
    last_skipped: int
    last_task_ids: list[str]
    last_error: str | None
    last_run_at: float | None
    created_at: float
    updated_at: float


def collection_url(url):
    url = validate_url(url)
    if provider(url) == 'jable' and not urlsplit(url).path.startswith('/latest-updates'):
        raise APIError(400, 40002, '采集任务需要列表页地址')
    return url


@app.get('/api/collections', dependencies=[Depends(authorize)])
def collection_list(request: Request):
    return dict(code=0, message='ok', data=request.app.state.collections.list())


@app.post('/api/collections', dependencies=[Depends(authorize)])
def collection_create(body: CollectionInput, request: Request):
    url = collection_url(body.url)
    name = (body.name or '').strip() or f'{provider(url)} 列表采集'
    return dict(code=0, message='ok', data=request.app.state.collections.create(
        name, url, body.batch_size, body.scheduled_enabled))


@app.put('/api/collections/{task_id}', dependencies=[Depends(authorize)])
def collection_update(task_id: str, body: CollectionInput, request: Request):
    from collection_tasks import CollectionError
    try:
        result = request.app.state.collections.update(task_id, (body.name or '').strip() or '列表采集',
                                                      collection_url(body.url), body.batch_size,
                                                      body.scheduled_enabled)
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


@app.post('/api/collections/bulk-execute', dependencies=[Depends(authorize)])
def collection_bulk_execute(body: BulkIdsInput, request: Request):
    return dict(code=0, message='queued', data=request.app.state.collections.execute_many(body.ids))


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
def collection_item_list(request: Request, page: int = Query(1, ge=1),
                         page_size: int = Query(20, ge=1, le=100),
                         title: str | None = Query(None, max_length=100),
                         source: str | None = Query(None), status: str | None = Query(None),
                         collection_id: str | None = Query(None, max_length=32)):
    if source and source not in {'jable', '123av'}:
        raise APIError(400, 40001, '来源筛选参数无效')
    if status and status not in {'pending', 'queued', 'downloading', 'completed', 'failed'}:
        raise APIError(400, 40001, '资源状态筛选参数无效')
    result = request.app.state.collections.list_items(page, page_size, title, source, status, collection_id)
    return dict(code=0, message='ok', data=result)


@app.post('/api/collection-items/{item_id}/download', dependencies=[Depends(authorize)])
def collection_item_download(item_id: str, request: Request):
    result = request.app.state.collections.schedule_item(item_id)
    if result is None:
        raise APIError(404, 40405, '采集资源不存在')
    return dict(code=0, message='queued', data=result)


@app.post('/api/collection-items/bulk-download', dependencies=[Depends(authorize)])
def collection_item_bulk_download(body: BulkIdsInput, request: Request):
    return dict(code=0, message='queued', data=request.app.state.collections.schedule_items(body.ids))


@app.post('/api/collection-items/download-all', dependencies=[Depends(authorize)])
def collection_item_download_all(request: Request):
    result = request.app.state.collections.enqueue_all_pending()
    return dict(code=0, message='queued', data=result)


class SchedulerSettingsInput(BaseModel):
    enabled: bool
    concurrency: int = Field(1, ge=1, le=10)


@app.get('/api/settings/scheduler', dependencies=[Depends(authorize)])
def scheduler_settings(request: Request):
    return dict(code=0, message='ok', data=request.app.state.collections.settings())


@app.put('/api/settings/scheduler', dependencies=[Depends(authorize)])
def scheduler_settings_update(body: SchedulerSettingsInput, request: Request):
    result = request.app.state.collections.update_settings(body.enabled, body.concurrency)
    return dict(code=0, message='ok', data=result)


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
