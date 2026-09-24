"""Persistent, single-worker HLS download queue. Files are committed atomically."""
import base64
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from urllib.parse import quote, urljoin, urlsplit

import m3u8
import requests
from Crypto.Cipher import AES
from bs4 import BeautifulSoup
from config import headers
from media_previews import MediaPreviewGenerator

log = logging.getLogger(__name__)


class DownloadError(Exception):
    pass


class Stopped(Exception):
    pass


class Cancelled(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.part')
    with temporary.open('wb') as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


class SpeedMeter:
    """Process-wide rolling bandwidth meter shared by every download type."""
    def __init__(self, window_seconds=5.0):
        self.window_seconds = float(window_seconds)
        self.lock = threading.Lock()
        self.samples = deque()
        self.window_bytes = 0

    def _trim(self, now):
        cutoff = now - self.window_seconds
        while self.samples and self.samples[0][0] <= cutoff:
            _, size = self.samples.popleft()
            self.window_bytes -= size

    def record(self, size):
        if size <= 0:
            return
        now = time.monotonic()
        with self.lock:
            self.samples.append((now, size))
            self.window_bytes += size
            self._trim(now)

    def speed(self):
        now = time.monotonic()
        with self.lock:
            self._trim(now)
            if not self.samples:
                return 0.0
            elapsed = min(self.window_seconds, max(1.0, now - self.samples[0][0]))
            return round(self.window_bytes / elapsed, 2)


class FileDownloader:
    """Shared network, retry, validation and atomic-file boundary."""
    def __init__(self, speed_meter, stop_event, cancel_check):
        self.speed_meter = speed_meter
        self.stop_event = stop_event
        self.cancel_check = cancel_check

    def fetch(self, task_id, transport, url, byte_range=None,
              limit=64 * 1024 * 1024, attempts=3, validator=None, resource_type=None):
        last_error = None
        for attempt in range(attempts):
            if self.stop_event.is_set():
                raise Stopped()
            self.cancel_check(task_id)
            try:
                data = transport.get(url, byte_range=byte_range, limit=limit,
                                     resource_type=resource_type)
                # Invalid responses still crossed the network and belong in the meter.
                self.speed_meter.record(len(data))
                self.cancel_check(task_id)
                if validator:
                    validator(data)
                return data
            except (Stopped, Cancelled):
                raise
            except Exception as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise
                if self.stop_event.wait(1):
                    raise Stopped()
        raise last_error

    @staticmethod
    def save(path, data):
        atomic_write(path, data)
        return {'path': str(path), 'size': len(data), 'hash': digest(data)}

    def download(self, task_id, transport, url, path, byte_range=None,
                 limit=64 * 1024 * 1024, attempts=3, validator=None, resource_type=None):
        data = self.fetch(task_id, transport, url, byte_range=byte_range,
                          limit=limit, attempts=attempts, validator=validator,
                          resource_type=resource_type)
        return self.save(path, data)


def asset_extension(data):
    if data.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return '.gif'
    if len(data) >= 12 and data[:4] in (b'RIFF', b'RIFX') and data[8:12] == b'WEBP':
        return '.webp'
    if len(data) >= 12 and data[4:8] == b'ftyp':
        return '.mp4'
    if data.startswith(b'\x1aE\xdf\xa3'):
        return '.webm'
    return None


def is_image(data):
    return asset_extension(data) in ('.jpg','.png','.gif','.webp')


def merge_card_metadata(metadata, item):
    if not item:
        return
    # Keep distinct source URLs even when cover and poster happen to be identical.
    for field in ('cover', 'preview_image'):
        if item.get(field):
            metadata[field] = item[field]


def enrich_123av_card(metadata, page_url):
    """Find the exact result card so a detail-only download also gets list assets."""
    import provider_123av
    video_id = metadata['id'].lower()
    page = urlsplit(page_url)
    first_segment = page.path.strip('/').split('/', 1)[0]
    locale = first_segment if re.fullmatch(r'[a-z]{2}', first_segment) else 'cn'
    search_url = f'https://{page.hostname}/{locale}/search?keyword=' + requests.utils.quote(video_id, safe='')
    try:
        response = requests.get(search_url, headers=headers, timeout=20)
        response.raise_for_status()
        result = provider_123av.parse_list(response.text, search_url, 1)
        item = next((x for x in result['items'] if x['id'].lower() == video_id), None)
        merge_card_metadata(metadata, item)
    except requests.RequestException:
        log.warning('Unable to enrich 123AV list assets for %s', video_id)


def enrich_jable_card(metadata):
    """Use an isolated browser so list lookup cannot disturb the video session."""
    import api
    from crawler import initWebDriver
    from selenium.webdriver.support.ui import WebDriverWait
    video_id = metadata['id'].lower()
    search_url = f'https://jable.tv/search/{video_id}/'
    driver = None
    try:
        driver = initWebDriver()
        driver.set_page_load_timeout(25)
        driver.get(search_url)
        api.validate_url(driver.current_url, list_search=True)
        def exact(browser):
            result = api.list_data(browser.page_source, search_url, 1)
            return next((x for x in result['items'] if x['id'].lower() == video_id), None)
        item = WebDriverWait(driver, 20).until(exact)
        merge_card_metadata(metadata, item)
    except Exception:
        log.warning('Unable to enrich Jable list assets for %s', video_id)
    finally:
        if driver is not None:
            driver.quit()


def public_url(url):
    """Do not let page/playlist resources address local services."""
    p = urlsplit(url)
    if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
        raise DownloadError('资源地址不允许')
    try:
        addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80))
    except OSError:
        raise DownloadError('资源域名解析失败')
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise DownloadError('资源地址指向非公网地址')
    return url


class HttpTransport:
    """Direct HLS resources, with the same public-network boundary as browser downloads."""
    def __init__(self, session, referer=None):
        self.session = session
        self.resolved_urls = {}
        self.headers = dict(headers)
        if referer:
            self.headers['Referer'] = referer

    def get(self, url, byte_range=None, limit=64 * 1024 * 1024,
            resource_type=None, extra_headers=None):
        original_url = url
        request_headers = dict(self.headers)
        request_headers.update(extra_headers or {})
        if byte_range:
            request_headers['Range'] = f'bytes={byte_range[0]}-{byte_range[0] + byte_range[1] - 1}'
        for _ in range(6):
            public_url(url)
            with self.session.get(url, headers=request_headers, timeout=(10, 30),
                                  stream=True, allow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('Location')
                    if not location:
                        raise DownloadError('资源重定向地址缺失')
                    url = urljoin(url, location)
                    continue
                if not 200 <= response.status_code < 300:
                    raise DownloadError(f'资源获取失败：HTTP {response.status_code}')
                if byte_range and response.status_code != 206:
                    raise DownloadError('资源不支持分片字节范围')
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > limit:
                        raise DownloadError('资源文件超过大小限制')
                    chunks.append(chunk)
                if byte_range and size != byte_range[1]:
                    raise DownloadError('分片字节范围长度不匹配')
                self.resolved_urls[original_url] = url
                return b''.join(chunks)
        raise DownloadError('资源重定向次数过多')

    def concurrent_factory(self):
        request_headers = dict(self.headers)
        cookies = list(self.session.cookies)
        def create():
            session = requests.Session()
            for cookie in cookies:
                session.cookies.set(cookie.name, cookie.value, domain=cookie.domain,
                                    path=cookie.path)
            transport = HttpTransport(session)
            transport.headers = dict(request_headers)
            return transport
        return create


class BrowserTransport:
    """Fetch with the same browser origin/session that resolved the stream."""
    def __init__(self, driver, cdp_fallback=False):
        self.driver = driver
        self.cdp_fallback = cdp_fallback
        self.concurrent_lock = threading.RLock()

    def open_123av_player(self, player_url):
        """Navigate to one chapter and resolve media while its origin is active."""
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.support.ui import WebDriverWait
        p = urlsplit(player_url)
        if (p.scheme != 'https' or p.hostname != 'javplayer.cc' or p.username or p.password
                or p.port not in (None, 443) or not re.fullmatch(r'/e/[A-Za-z0-9]+', p.path)):
            raise DownloadError('播放器地址不支持')
        target = 'https://javplayer.cc' + p.path
        try:
            self.driver.get(target)
        except TimeoutException:
            pass
        def loaded(browser):
            if urlsplit(browser.current_url).hostname != 'javplayer.cc':
                raise DownloadError('播放器跳转到不支持的地址')
            resources = browser.execute_script(
                "return performance.getEntriesByType('resource').map(x=>x.name)")
            stream = next((u for u in resources if urlsplit(u).path.endswith('.m3u8')), None)
            if not stream:
                return None
            vtt = next((u for u in resources if urlsplit(u).path.lower().endswith('.vtt')), None)
            return {'m3u8_url': stream, 'vtt_url': vtt}
        return WebDriverWait(self.driver, 30).until(loaded)

    def cdp_get(self, url, limit):
        host = (urlsplit(url).hostname or '').lower()
        allowed = host == 'assets-cdn.jable.tv' or host.endswith('.mushroomtrack.com')
        if not self.cdp_fallback or not allowed:
            raise DownloadError('资源跨域读取失败')
        frame = self.driver.execute_cdp_cmd('Page.getFrameTree',{})['frameTree']['frame']['id']
        response = self.driver.execute_cdp_cmd('Network.loadNetworkResource', {
            'frameId':frame, 'url':url,
            'options':{'disableCache':False,'includeCredentials':True}
        }).get('resource',{})
        if not response.get('success') or not 200 <= int(response.get('httpStatusCode',0)) < 300:
            raise DownloadError('浏览器网络接口获取资源失败')
        try:
            declared = int(response.get('headers',{}).get('content-length','0'))
        except ValueError:
            declared = 0
        if declared > limit:
            raise DownloadError('资源超过大小限制')
        handle = response.get('stream')
        if not handle:
            raise DownloadError('浏览器网络接口没有返回资源流')
        chunks, size = [], 0
        try:
            while True:
                part = self.driver.execute_cdp_cmd('IO.read',{'handle':handle,'size':65536})
                raw = (base64.b64decode(part['data']) if part.get('base64Encoded')
                       else part.get('data','').encode('latin1'))
                size += len(raw)
                if size > limit:
                    raise DownloadError('资源超过大小限制')
                chunks.append(raw)
                if part.get('eof'):
                    break
        finally:
            self.driver.execute_cdp_cmd('IO.close',{'handle':handle})
        return b''.join(chunks)

    def get(self, url, byte_range=None, limit=64 * 1024 * 1024,
            resource_type=None, extra_headers=None):
        public_url(url)
        result = self.driver.execute_async_script("""
          const [url, range, limit, extraHeaders, done] = arguments;
          const controller = new AbortController();
          const timer = setTimeout(()=>controller.abort(), 30000);
          (async()=>{
            const requestHeaders = Object.assign({}, extraHeaders || {});
            if(range) requestHeaders.Range='bytes='+range[0]+'-'+(range[0]+range[1]-1);
            const r = await fetch(url, {redirect:'error', signal:controller.signal, headers:requestHeaders});
            if (!r.ok) throw new Error('HTTP '+r.status);
            if (range && r.status !== 206) throw new Error('Range unsupported');
            const reader=r.body.getReader(); let size=0, chunks=[];
            while(true) {const {done,value}=await reader.read(); if(done)break;
              size+=value.length; if(size>limit){await reader.cancel();throw new Error('Resource too large');}
              chunks.push(value);}
            let binary=''; for(const chunk of chunks)for(let i=0;i<chunk.length;i+=32768)
              binary+=String.fromCharCode(...chunk.subarray(i,i+32768));
            done({data:btoa(binary)});
          })().catch(e=>done({error:String(e).slice(0,120)})).finally(()=>clearTimeout(timer));
        """, url, byte_range, limit, extra_headers or {})
        if not result or 'error' in result:
            if byte_range is None and not extra_headers:
                return self.cdp_get(url,limit)
            raise DownloadError('资源获取失败：' + ((result or {}).get('error', '浏览器无响应')))
        data = base64.b64decode(result['data'])
        if byte_range and len(data) != byte_range[1]:
            raise DownloadError('分片字节范围长度不匹配')
        return data

    def concurrent_factory(self):
        """Use direct HTTP per worker and serialize only browser fallbacks."""
        referer = self.driver.current_url
        cookies = self.driver.get_cookies()
        try:
            user_agent = self.driver.execute_script('return navigator.userAgent')
        except Exception:
            user_agent = headers.get('User-Agent')
        parent = self
        def create():
            session = requests.Session()
            for cookie in cookies:
                options = {'path': cookie.get('path') or '/'}
                if cookie.get('domain'):
                    options['domain'] = cookie['domain']
                session.cookies.set(cookie['name'], cookie['value'], **options)
            direct = HttpTransport(session, referer=referer)
            if user_agent:
                direct.headers['User-Agent'] = user_agent
            return ConcurrentBrowserTransport(direct, parent)
        return create


class ConcurrentBrowserTransport:
    def __init__(self, direct, parent):
        self.direct, self.parent = direct, parent
        self.resolved_urls = {}

    def get(self, url, byte_range=None, limit=64 * 1024 * 1024,
            resource_type=None, extra_headers=None):
        try:
            data = self.direct.get(url, byte_range=byte_range, limit=limit,
                                   resource_type=resource_type,
                                   extra_headers=extra_headers)
            self.resolved_urls.update(self.direct.resolved_urls)
            return data
        except (Stopped, Cancelled):
            raise
        except Exception:
            # Selenium/CDP is not thread-safe. Sources that reject direct HTTP
            # still work through the original browser, one fallback at a time.
            with self.parent.concurrent_lock:
                return self.parent.get(url, byte_range=byte_range, limit=limit,
                                       resource_type=resource_type,
                                       extra_headers=extra_headers)


class SigningTransport:
    """Apply the current source signer immediately before each network request."""
    def __init__(self, transport, signers, task):
        self.transport, self.signers, self.task = transport, signers, task
        self.resolved_urls = {}

    def get(self, url, byte_range=None, limit=64 * 1024 * 1024, resource_type=None):
        from download_signers import SignerError
        context = dict(task_id=self.task['id'], video_id=self.task['video_id'],
                       resource_type=resource_type or 'resource', referer=self.task.get('url'))
        try:
            signed = self.signers.sign(self.task['source'], url, context)
        except SignerError as exc:
            raise DownloadError(str(exc)) from None
        data = self.transport.get(signed['url'], byte_range=byte_range, limit=limit,
                                  resource_type=resource_type, extra_headers=signed['headers'])
        # Keep redirects and a signer-selected host/path for relative HLS resolution.
        # DownloadManager persists only the plan fingerprint, so this URL stays in memory.
        self.resolved_urls[url] = getattr(self.transport, 'resolved_urls', {}).get(
            signed['url'], signed['url'])
        return data

    def open_123av_player(self, player_url):
        """Keep provider-specific browser navigation available through the signer wrapper."""
        opener = getattr(self.transport, 'open_123av_player', None)
        if not opener:
            raise DownloadError('当前下载传输不支持切换章节播放器')
        return opener(player_url)

    def concurrent_factory(self):
        base_factory = self.transport.concurrent_factory()
        signers, task = self.signers, self.task
        return lambda: SigningTransport(base_factory(), signers, task)


@contextmanager
def browser_source(url):
    # Lazy import avoids a circular import and browser creation on module import.
    import api
    from crawler import initWebDriver
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.common.exceptions import TimeoutException
    url = api.validate_url(url, detail=True)
    driver = initWebDriver()
    driver.set_page_load_timeout(30)
    driver.set_script_timeout(40)
    try:
        if api.provider(url) == '123av':
            source, final = api.fetch_page(url, detail=True)
            metadata = api.detail_data(source, final)
            enrich_123av_card(metadata, final)
            chapters = api.provider_123av.player_chapters(source)
            if not chapters:
                raise DownloadError('未找到播放器')
            metadata['chapters'] = [dict(index=item['index'], title=item['title'],
                                         duration=item.get('duration'),
                                         _player_url=item['player_url']) for item in chapters]
            target = None
        else:
            target, metadata = url, None
        transport = BrowserTransport(driver, cdp_fallback=api.provider(url) == 'jable')
        if api.provider(url) == '123av':
            first = transport.open_123av_player(metadata['chapters'][0]['_player_url'])
            metadata['chapters'][0].update(first)
            metadata['m3u8_url'] = first['m3u8_url']
            metadata['vtt_url'] = first.get('vtt_url')
        else:
            try:
                driver.get(target)
            except TimeoutException:
                pass  # Ads may time out after the video has loaded.

        def loaded(browser):
            nonlocal metadata
            if api.provider(url) == 'jable':
                api.validate_url(browser.current_url, detail=True)
                metadata = api.detail_data(browser.page_source, url)
                return metadata.get('m3u8_url')
            return metadata.get('m3u8_url')
        if api.provider(url) == 'jable':
            WebDriverWait(driver, 30).until(loaded)
        if api.provider(url) == 'jable':
            enrich_jable_card(metadata)
        yield metadata, transport
    finally:
        driver.quit()


def build_plan(file_downloader, task_id, transport, stream_url):
    """Pick the highest resolution and preserve keys, maps and byte ranges."""
    def playlist(url):
        def validate(data):
            if not data.decode('utf-8-sig').lstrip().startswith('#EXTM3U'):
                raise DownloadError('来源没有返回有效 m3u8')
        raw = file_downloader.fetch(task_id, transport, url, limit=4 * 1024 * 1024,
                                    validator=validate, resource_type='playlist').decode('utf-8-sig')
        return m3u8.loads(raw)

    master_url = stream_url
    top = playlist(stream_url)
    stream_url = getattr(transport, 'resolved_urls', {}).get(stream_url, stream_url)
    master_url = stream_url
    tracks = [('video', stream_url, top)]
    master = None
    if top.is_variant:
        if not top.playlists:
            raise DownloadError('主清单没有视频流')
        chosen = max(top.playlists, key=lambda p: ((p.stream_info.resolution or (0,0))[0] * (p.stream_info.resolution or (0,0))[1], p.stream_info.bandwidth or 0))
        stream_url = urljoin(stream_url, chosen.uri)
        video_playlist = playlist(stream_url)
        stream_url = getattr(transport, 'resolved_urls', {}).get(stream_url, stream_url)
        tracks = [('video', stream_url, video_playlist)]
        audio = [a for a in top.media if a.type == 'AUDIO' and a.group_id == chosen.stream_info.audio and a.uri]
        if audio:
            selected = next((a for a in audio if a.default == 'YES'), audio[0])
            audio_url = urljoin(master_url, selected.uri)
            audio_playlist = playlist(audio_url)
            audio_url = getattr(transport, 'resolved_urls', {}).get(audio_url, audio_url)
            tracks.append(('audio', audio_url, audio_playlist))
        master = chosen.stream_info

    resources, manifests = [], {}
    multiple_tracks = len(tracks) > 1
    for track, location, data in tracks:
        if data.is_variant or not data.is_endlist or not data.segments:
            raise DownloadError('仅支持已结束的点播清单，不支持直播或嵌套主清单')
        if any(getattr(s, 'gap_tag', None) for s in data.segments):
            raise DownloadError('来源清单包含缺失分片')
        key_paths, map_paths = {}, {}
        offset, previous_uri = 0, None
        for i, segment in enumerate(data.segments):
            key_id = None
            if segment.key and segment.key.method != 'NONE':
                key = segment.key
                if key.method != 'AES-128' or key.keyformat not in (None, 'identity'):
                    raise DownloadError('不支持此清单的加密方式')
                original = urljoin(location, key.uri)
                if original not in key_paths:
                    path = f'keys/{track}-{len(key_paths):04d}.key'
                    key_paths[original] = path
                    resources.append(dict(path=path, kind='key', url=original, byte_range=None))
                key_id = key_paths[original]
            init = getattr(segment, 'init_section', None)
            if init:
                if init.byterange:
                    raise DownloadError('暂不支持带字节范围的初始化片段')
                original = urljoin(location, init.uri)
                if original not in map_paths:
                    prefix = f'{track}-' if multiple_tracks else ''
                    path = f'{prefix}init-{len(map_paths):04d}.ts'
                    map_paths[original] = path
                    resources.append(dict(path=path, kind='init', url=original, byte_range=None))
            original = urljoin(location, segment.uri)
            byte_range = None
            if segment.byterange:
                parts = segment.byterange.split('@')
                count = int(parts[0])
                if len(parts) == 2:
                    offset = int(parts[1])
                elif previous_uri != original:
                    raise DownloadError('分片字节范围缺少起始位置')
                byte_range = [offset, count]
                offset += count
            previous_uri = original
            prefix = f'{track}-' if multiple_tracks else ''
            segment_path = f'{prefix}{i:04d}.ts'
            resources.append(dict(path=segment_path, kind='segment', url=original,
                                  byte_range=byte_range, duration=segment.duration,
                                  sequence=int(data.media_sequence or 0)+i, key=key_id,
                                  iv=segment.key.iv if key_id else None,
                                  discontinuity=bool(segment.discontinuity)))
        # Mutate only after collecting original URLs (key/map objects are shared).
        for i, segment in enumerate(data.segments):
            prefix = f'{track}-' if multiple_tracks else ''
            segment.uri = f'{prefix}{i:04d}.ts'
            segment.byterange = None
        for key in data.keys:
            if key and key.method != 'NONE':
                key.uri = key_paths[urljoin(location, key.uri)]
        visited = set()
        for segment in data.segments:
            init = getattr(segment, 'init_section', None)
            if init and id(init) not in visited:
                visited.add(id(init))
                init.uri = map_paths[urljoin(location, init.uri)]
        manifests[track+'.m3u8' if len(tracks)>1 else 'index.m3u8'] = data.dumps()
    if len(tracks)>1:
        manifests['index.m3u8'] = ('#EXTM3U\n#EXT-X-VERSION:6\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="audio",DEFAULT=YES,AUTOSELECT=YES,URI="audio.m3u8"\n'
            f'#EXT-X-STREAM-INF:BANDWIDTH={master.bandwidth or 1},AUDIO="audio"\nvideo.m3u8\n')
    signature = [{k:v for k,v in r.items() if k != 'url'} | {'name':Path(urlsplit(r['url']).path).name} for r in resources]
    return dict(resources=resources, manifests=manifests, fingerprint=digest(json.dumps(signature,sort_keys=True).encode()))


def validate_payload(data, resource, folder):
    if resource['kind'] == 'key':
        if len(data) != 16:
            raise DownloadError('AES 密钥长度错误')
        return
    check = data
    if resource.get('key'):
        if len(data) % 16:
            raise DownloadError('加密分片不完整')
        iv = int(resource['iv'],16) if resource.get('iv') else resource['sequence']
        key = (folder / resource['key']).read_bytes()
        check = AES.new(key, AES.MODE_CBC, iv.to_bytes(16,'big')).decrypt(data)
    # Inspect bytes, not Content-Type or a deceptive .css/.js extension.
    ts = len(check) >= 376 and check[0] == 0x47 and check[188] == 0x47
    mp4 = len(check) >= 8 and check[4:8] in (b'ftyp',b'styp',b'moof',b'moov',b'sidx')
    aac = check.startswith(b'ID3') or (len(check)>2 and check[0]==255 and check[1]&0xf6==0xf0)
    if not (ts or mp4 or aac):
        raise DownloadError('分片不是可识别的视频/音频内容，可能返回了错误页')


class DownloadManager:
    MAX_CONCURRENCY = 10
    MAX_SEGMENT_CONCURRENCY = 16

    def __init__(self, root, resolver=browser_source, start_enabled=True, database=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        environment_url = os.getenv('PUBLIC_BASE_URL')
        self.environment_public_base_url = self.normalize_public_base_url(
            environment_url or 'http://localhost:8000')
        self.environment_public_base_url_source = (
            'environment' if environment_url
            and self.environment_public_base_url != 'http://localhost:8000' else 'default')
        self.public_base_url = self.environment_public_base_url
        self.public_base_url_source = self.environment_public_base_url_source
        self.public_base_url_updated_at = None
        self.database = Path(database).resolve() if database else self.root / 'tasks.sqlite3'
        self.segment_concurrency = 3
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.resolver = resolver
        self.signers = None
        self.completion_callback = None
        self.stop_event, self.wake = threading.Event(), threading.Event()
        self.cancelled = set()
        self.dispatch_lock = threading.Lock()
        self.active_condition = threading.Condition()
        self.active_tasks = set()
        self.concurrency = 1
        self.dispatch_enabled = bool(start_enabled)
        self.lockfile = (self.root / '.worker.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.lockfile.seek(0); self.lockfile.write(b'0'); self.lockfile.flush(); self.lockfile.seek(0)
                msvcrt.locking(self.lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lockfile.close()
            raise RuntimeError('下载目录已有服务运行，请使用单个 API worker')
        with self.db() as db:
            # Downloads continuously write segment progress. WAL lets API reads
            # proceed without waiting for those short write transactions.
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=NORMAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY, collection_id TEXT, source TEXT NOT NULL,
                  video_id TEXT NOT NULL, title TEXT, title_key TEXT, url TEXT NOT NULL,
                  release_date TEXT, duration TEXT, cover_url TEXT, poster_file TEXT,
                  preview_url TEXT, preview_file TEXT, vtt_url TEXT, vtt_file TEXT,
                  m3u8_url TEXT, preview_m3u8_url TEXT, preview_m3u8_file TEXT,
                  preview5_m3u8_url TEXT, preview5_m3u8_file TEXT,
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  status TEXT NOT NULL DEFAULT 'pending', message TEXT, error TEXT,
                  warnings TEXT NOT NULL DEFAULT '[]', plan TEXT,
                  r2_synced INTEGER NOT NULL DEFAULT 0,
                  r2_sync_status TEXT NOT NULL DEFAULT 'unsynced',
                  r2_synced_at REAL,
                  local_deleted INTEGER NOT NULL DEFAULT 0,
                  local_deleted_at REAL,
                  assets_refresh INTEGER NOT NULL DEFAULT 0,
                  total_segments INTEGER NOT NULL DEFAULT 0,
                  downloaded_segments INTEGER NOT NULL DEFAULT 0,
                  downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                  first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                  created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS files (
                  task_id TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
                  size INTEGER NOT NULL DEFAULT 0, hash TEXT, PRIMARY KEY(task_id,path));
                CREATE INDEX IF NOT EXISTS files_task_kind ON files(task_id,kind);
                CREATE UNIQUE INDEX IF NOT EXISTS tasks_video_id_unique ON tasks(lower(trim(video_id)));
                CREATE UNIQUE INDEX IF NOT EXISTS tasks_title_key_unique
                  ON tasks(title_key) WHERE title_key IS NOT NULL AND title_key<>'';
                CREATE INDEX IF NOT EXISTS tasks_status_created ON tasks(status,created);
                CREATE INDEX IF NOT EXISTS tasks_collection ON tasks(collection_id,last_seen);
                CREATE TABLE IF NOT EXISTS app_settings (
                  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL);
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(tasks)').fetchall()}
            for name in ('preview_m3u8_url', 'preview_m3u8_file',
                         'preview5_m3u8_url', 'preview5_m3u8_file'):
                if name not in columns:
                    db.execute(f'ALTER TABLE tasks ADD COLUMN {name} TEXT')
            if 'r2_synced' not in columns:
                db.execute('''ALTER TABLE tasks ADD COLUMN
                              r2_synced INTEGER NOT NULL DEFAULT 0''')
            if 'r2_sync_status' not in columns:
                db.execute("""ALTER TABLE tasks ADD COLUMN
                              r2_sync_status TEXT NOT NULL DEFAULT 'unsynced'""")
            if 'r2_synced_at' not in columns:
                db.execute('ALTER TABLE tasks ADD COLUMN r2_synced_at REAL')
            if 'local_deleted' not in columns:
                db.execute('ALTER TABLE tasks ADD COLUMN local_deleted INTEGER NOT NULL DEFAULT 0')
            if 'local_deleted_at' not in columns:
                db.execute('ALTER TABLE tasks ADD COLUMN local_deleted_at REAL')
            db.execute("""UPDATE tasks SET r2_sync_status=CASE
                          WHEN r2_synced=1 THEN 'synced' ELSE 'unsynced' END
                          WHERE r2_sync_status IS NULL OR r2_sync_status NOT IN
                          ('unsynced','syncing','synced')""")
            db.execute("UPDATE tasks SET r2_sync_status='synced' WHERE r2_synced=1")
            db.execute("UPDATE tasks SET status='queued',updated=? WHERE status='downloading'", (time.time(),))
            db.execute('UPDATE tasks SET assets_refresh=1 WHERE assets_refresh=2')
            db.execute('UPDATE tasks SET assets_refresh=3 WHERE assets_refresh=4')
            queued_chapter_previews = self.queue_missing_chapter_previews(db)
            if queued_chapter_previews:
                log.info('Queued chapter preview backfill for %s completed tasks',
                         queued_chapter_previews)
            public_setting = db.execute(
                "SELECT value,updated FROM app_settings WHERE key='public_base_url'").fetchone()
            if public_setting:
                try:
                    self.public_base_url = self.normalize_public_base_url(public_setting['value'])
                    self.public_base_url_source = 'dynamic'
                    self.public_base_url_updated_at = public_setting['updated']
                except ValueError:
                    log.warning('Ignoring invalid stored public_base_url')
            self.rebuild_progress(db)
        self.speed_meter = SpeedMeter(window_seconds=5.0)
        self.file_downloader = FileDownloader(self.speed_meter, self.stop_event, self.check_cancelled)
        try:
            preview_timeout = int(os.getenv('DOWNLOAD_PREVIEW_TIMEOUT',
                                             os.getenv('UPLOAD_VIDEO_TIMEOUT', '3600')))
            preview_interval = int(os.getenv('DOWNLOAD_PREVIEW_INTERVAL',
                                              os.getenv('UPLOAD_PREVIEW_INTERVAL', '10')))
        except ValueError:
            preview_timeout, preview_interval = 3600, 10
        self.preview_generator = MediaPreviewGenerator(preview_timeout, preview_interval)
        self.threads = [threading.Thread(target=self.run, args=(index,), name=f'download-worker-{index + 1}', daemon=True)
                        for index in range(self.MAX_CONCURRENCY)]
        for thread in self.threads:
            thread.start()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def queue_missing_chapter_previews(db):
        """Queue a one-time asset refresh for completed legacy multi-chapter tasks."""
        migration_key = 'chapter_preview_backfill_v1'
        if db.execute('SELECT 1 FROM app_settings WHERE key=?',
                      (migration_key,)).fetchone():
            return 0
        queued = 0
        rows = db.execute('''SELECT id,metadata_json FROM tasks
                             WHERE status='completed'
                               AND metadata_json LIKE '%"chapters"%' ''').fetchall()
        for row in rows:
            try:
                metadata = json.loads(row['metadata_json'] or '{}')
            except (TypeError, ValueError):
                continue
            chapters = metadata.get('chapters')
            if not isinstance(chapters, list) or len(chapters) < 2:
                continue
            missing = any(
                isinstance(chapter, dict) and chapter.get('m3u8_local')
                and (not chapter.get('preview_m3u8_local')
                     or not chapter.get('preview5_m3u8_local'))
                for chapter in chapters
            )
            if missing:
                # Mode 3 only rebuilds short playlists from local chapter files.
                # It must not visit a source site, which may be unavailable after
                # the video itself has already completed successfully.
                db.execute('UPDATE tasks SET assets_refresh=3 WHERE id=?', (row['id'],))
                queued += 1
        now = time.time()
        db.execute('''INSERT INTO app_settings(key,value,updated) VALUES(?,?,?)
                      ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                      updated=excluded.updated''', (migration_key, str(queued), now))
        return queued

    @staticmethod
    def rebuild_progress(db, task_id=None):
        where = 'WHERE id=?' if task_id else ''
        values = (task_id,) if task_id else ()
        db.execute(f'''UPDATE tasks SET
            total_segments=(SELECT count(*) FROM files f WHERE f.task_id=tasks.id AND f.kind='segment'),
            downloaded_segments=(SELECT count(*) FROM files f WHERE f.task_id=tasks.id
                                  AND f.kind='segment' AND f.hash IS NOT NULL),
            downloaded_bytes=(SELECT coalesce(sum(f.size),0) FROM files f WHERE f.task_id=tasks.id
                              AND f.kind='segment' AND f.hash IS NOT NULL)
            {where}''', values)

    def close(self):
        self.stop_event.set(); self.wake.set()
        deadline = time.monotonic() + 45
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if not any(thread.is_alive() for thread in self.threads):
            self.lockfile.close()

    def set_concurrency(self, value):
        value = max(1, min(self.MAX_CONCURRENCY, int(value)))
        with self.active_condition:
            self.concurrency = value
            self.active_condition.notify_all()
        self.wake.set()

    def set_scheduler(self, enabled, concurrency, segment_concurrency=None):
        value = max(1, min(self.MAX_CONCURRENCY, int(concurrency)))
        with self.active_condition:
            self.dispatch_enabled = bool(enabled)
            self.concurrency = value
            if segment_concurrency is not None:
                self.segment_concurrency = max(
                    1, min(self.MAX_SEGMENT_CONCURRENCY, int(segment_concurrency)))
            self.active_condition.notify_all()
        self.wake.set()

    def running_count(self):
        with self.active_condition:
            return len(self.active_tasks)

    def scheduled_count(self):
        with self.db() as db:
            return db.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','downloading')").fetchone()[0]

    def status_counts(self):
        with self.db() as db:
            row = db.execute('''SELECT count(*) total,
                                sum(CASE WHEN status IN ('queued','downloading') THEN 1 ELSE 0 END) active,
                                sum(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed,
                                sum(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed
                                FROM tasks''').fetchone()
        return {key: int(row[key] or 0) for key in ('total', 'active', 'completed', 'failed')}

    def download_speed(self):
        return self.speed_meter.speed()

    def notify_completed(self, task, metadata, folder):
        if not self.completion_callback:
            return
        try:
            self.completion_callback(dict(task), dict(metadata), Path(folder))
        except Exception:
            log.exception('Unable to enqueue completed download %s for synchronization',
                          task.get('id'))

    def is_cancelled(self, task_id):
        with self.active_condition:
            return task_id in self.cancelled

    def check_cancelled(self, task_id):
        if self.is_cancelled(task_id):
            raise Cancelled()

    @staticmethod
    def source_directory(source):
        """Keep provider grouping while using the public disk directory name."""
        source = str(source).strip().lower()
        return '123' if source == '123av' else 'jable' if source == 'jable' else 'external/' + source

    @staticmethod
    def normalize_public_base_url(value):
        value = str(value or '').strip().rstrip('/')
        try:
            parts = urlsplit(value)
            valid = (parts.scheme in ('http', 'https') and bool(parts.hostname)
                     and not parts.username and not parts.password
                     and parts.path in ('', '/') and not parts.query and not parts.fragment
                     and not re.search(r'[\s\\\x00-\x1f]', value))
            parts.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('公开访问地址必须是 HTTP/HTTPS 根地址，例如 http://localhost:8000')
        return value

    def public_url_settings(self):
        return dict(url=self.public_base_url, source=self.public_base_url_source,
                    updated_at=self.public_base_url_updated_at,
                    environment_url=self.environment_public_base_url,
                    default_url='http://localhost:8000')

    def set_public_base_url(self, value):
        normalized = self.normalize_public_base_url(value)
        updated = time.time()
        with self.db() as db:
            db.execute('''INSERT INTO app_settings(key,value,updated) VALUES('public_base_url',?,?)
                          ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated=excluded.updated''',
                       (normalized, updated))
        self.public_base_url = normalized
        self.public_base_url_source = 'dynamic'
        self.public_base_url_updated_at = updated
        return self.public_url_settings()

    def task_folder(self, source, video_id):
        folder = (self.root / self.source_directory(source) / str(video_id)).resolve()
        if folder == self.root or self.root not in folder.parents:
            raise DownloadError('任务资源目录不安全')
        return folder

    def _remove_task_folders(self, rows):
        """Remove verified task directories, preferring native rm inside Linux containers."""
        folders = {row['id']: self.task_folder(row['source'], row['video_id']) for row in rows}
        existing = [folder for folder in folders.values() if folder.exists()]
        if existing and os.name == 'posix':
            executable = shutil.which('rm')
            if executable:
                try:
                    subprocess.run([executable, '-rf', '--', *(str(folder) for folder in existing)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, check=False)
                except OSError:
                    log.warning('Native directory deletion failed to start', exc_info=True)
        for folder in existing:
            if not folder.exists():
                continue
            try:
                shutil.rmtree(folder)
            except OSError:
                log.warning('Unable to delete task directory %s', folder, exc_info=True)
        return folders

    def delete(self, task_id):
        result = self.delete_many([task_id])
        if result['missing']:
            return None
        if result['failed']:
            raise DownloadError(result['failures'][0]['message'])
        folder = result['deleted_paths'].get(task_id, '')
        return dict(task_id=task_id, deleted=True, deleted_path=folder)

    def delete_many(self, task_ids):
        task_ids = list(dict.fromkeys(task_ids))
        if not task_ids:
            return {
                'total': 0, 'deleted': 0, 'missing': 0, 'failed': 0,
                'deleted_ids': [], 'missing_ids': [], 'failures': [], 'deleted_paths': {},
            }
        placeholders = ','.join('?' for _ in task_ids)
        failed = []
        with self.dispatch_lock:
            with self.db() as db:
                rows = db.execute(
                    f'SELECT id,source,video_id,status,message FROM tasks WHERE id IN ({placeholders})',
                    task_ids).fetchall()
                rows_by_id = {row['id']: row for row in rows}
                missing = [task_id for task_id in task_ids if task_id not in rows_by_id]
            with self.active_condition:
                active = set(self.active_tasks)
            removable = []
            for task_id in task_ids:
                row = rows_by_id.get(task_id)
                if not row:
                    continue
                if task_id in active or row['status'] == 'downloading':
                    failed.append({'id': task_id, 'message': '任务正在下载，不能删除'})
                else:
                    removable.append(row)
            if removable:
                removable_ids = [row['id'] for row in removable]
                removable_placeholders = ','.join('?' for _ in removable_ids)
                with self.db() as db:
                    db.execute(
                        f"UPDATE tasks SET status='deleting',assets_refresh=0,updated=? "
                        f'WHERE id IN ({removable_placeholders})',
                        (time.time(), *removable_ids))

        folders = self._remove_task_folders(removable)
        deleted = []
        for row in removable:
            if folders[row['id']].exists():
                failed.append({'id': row['id'], 'message': '资源文件夹删除失败'})
            else:
                deleted.append(row['id'])

        with self.db() as db:
            if deleted:
                deleted_placeholders = ','.join('?' for _ in deleted)
                db.execute(f'DELETE FROM files WHERE task_id IN ({deleted_placeholders})', deleted)
                db.execute(f'DELETE FROM tasks WHERE id IN ({deleted_placeholders})', deleted)
            failed_ids = {item['id'] for item in failed}
            for row in removable:
                if row['id'] in failed_ids:
                    db.execute('UPDATE tasks SET status=?,message=?,updated=? WHERE id=?',
                               (row['status'], row['message'], time.time(), row['id']))
        return {
            'total': len(task_ids),
            'deleted': len(deleted),
            'missing': len(missing),
            'failed': len(failed),
            'deleted_ids': deleted,
            'missing_ids': missing,
            'failures': failed,
            'deleted_paths': {task_id: str(folders[task_id]) for task_id in deleted},
        }

    def submit(self, source, video_id, url):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # A video code identifies the same work across supported providers.
            row = db.execute('SELECT * FROM tasks WHERE lower(trim(video_id))=lower(trim(?)) ORDER BY created LIMIT 1',
                             (video_id,)).fetchone()
            if row:
                task_id = row['id']
                if row['status'] in ('pending', 'failed'):
                    db.execute("UPDATE tasks SET status='queued',message='等待下载',error=NULL,url=?,updated=? WHERE id=?",
                               (url,time.time(),task_id))
                elif row['status'] == 'completed':
                    # Keep completed/100% visible while the worker checks missing optional assets.
                    db.execute("UPDATE tasks SET assets_refresh=1,url=?,updated=? WHERE id=?",(url,time.time(),task_id))
            else:
                task_id = uuid.uuid4().hex
                now = time.time()
                db.execute('''INSERT INTO tasks(id,source,video_id,url,status,message,first_seen,last_seen,created,updated)
                              VALUES(?,?,?,?,?,?,?,?,?,?)''',
                           (task_id,source,video_id,url,'queued','等待下载',now,now,now,now))
        self.wake.set()
        return self.get(task_id)

    def submit_many(self, entries):
        results = []
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for entry in entries:
                row = db.execute('''SELECT * FROM tasks WHERE lower(trim(video_id))=lower(trim(?))
                                    ORDER BY created LIMIT 1''', (entry['video_id'],)).fetchone()
                if row:
                    task_id, status = row['id'], row['status']
                    if status in ('pending', 'failed'):
                        status = 'queued'
                        db.execute("UPDATE tasks SET status='queued',message='等待下载',error=NULL,url=?,updated=? WHERE id=?",
                                   (entry['url'], time.time(), task_id))
                else:
                    task_id, status = uuid.uuid4().hex, 'queued'
                    now = time.time()
                    db.execute('''INSERT INTO tasks(id,source,video_id,url,status,message,
                                  first_seen,last_seen,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                               (task_id, entry['source'], entry['video_id'], entry['url'], status,
                                '等待下载', now, now, now, now))
                results.append(dict(item_id=entry.get('item_id'), task_id=task_id, status=status))
        self.wake.set()
        return results

    def media_url(self, source, video_id, path):
        if not path:
            return None
        source_path = quote(self.source_directory(source), safe='/')
        video_path = quote(str(video_id), safe='')
        file_path = quote(str(path).replace('\\', '/'), safe='/')
        return f'{self.public_base_url}/{source_path}/{video_path}/{file_path}'

    def save_metadata(self, db, task_id, metadata):
        task = db.execute('SELECT source,video_id FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not task:
            raise DownloadError('下载任务不存在')
        poster_file = metadata.get('poster_local') or metadata.get('cover_local')
        cover_file = metadata.get('cover_local') or poster_file
        preview_file = metadata.get('preview_image_local')
        vtt_file = metadata.get('vtt_local')
        m3u8_file = metadata.get('m3u8_local')
        preview_m3u8_file = metadata.get('preview_m3u8_local')
        preview5_m3u8_file = metadata.get('preview5_m3u8_local')
        db.execute('''UPDATE tasks SET title=coalesce(?,title),release_date=coalesce(?,release_date),
                      duration=coalesce(?,duration),cover_url=coalesce(?,cover_url),
                      poster_file=coalesce(?,poster_file),preview_url=coalesce(?,preview_url),
                      preview_file=coalesce(?,preview_file),vtt_url=coalesce(?,vtt_url),
                      vtt_file=coalesce(?,vtt_file),m3u8_url=coalesce(?,m3u8_url),
                      preview_m3u8_url=coalesce(?,preview_m3u8_url),
                      preview_m3u8_file=coalesce(?,preview_m3u8_file),
                      preview5_m3u8_url=coalesce(?,preview5_m3u8_url),
                      preview5_m3u8_file=coalesce(?,preview5_m3u8_file),metadata_json=? WHERE id=?''',
                   (metadata.get('title'), metadata.get('release_date') or metadata.get('published_at'),
                    metadata.get('duration'), self.media_url(task['source'], task['video_id'], cover_file) or metadata.get('cover'),
                    poster_file, self.media_url(task['source'], task['video_id'], preview_file) or metadata.get('preview_image'),
                    preview_file, self.media_url(task['source'], task['video_id'], vtt_file) or metadata.get('vtt_url'), vtt_file,
                    self.media_url(task['source'], task['video_id'], m3u8_file) or metadata.get('m3u8_url'),
                    self.media_url(task['source'], task['video_id'], preview_m3u8_file) or metadata.get('preview_m3u8_url'),
                    preview_m3u8_file,
                    self.media_url(task['source'], task['video_id'], preview5_m3u8_file) or metadata.get('preview5_m3u8_url'),
                    preview5_m3u8_file,
                    json.dumps(metadata,ensure_ascii=False), task_id))

    def serialize_task(self, row, db=None):
        total, done = row['total_segments'] or 0, row['downloaded_segments'] or 0
        folder = self.task_folder(row['source'], row['video_id'])
        try:
            metadata = json.loads(row['metadata_json'] or '{}')
        except (ValueError, TypeError):
            metadata = {}
        chapters = []
        raw_chapters = metadata.get('chapters') if isinstance(metadata.get('chapters'), list) else []
        if raw_chapters:
            connection = db
            close_connection = False
            if connection is None:
                connection = sqlite3.connect(self.database, timeout=30)
                connection.row_factory = sqlite3.Row
                close_connection = True
            try:
                for index, chapter in enumerate(raw_chapters, 1):
                    prefix = f'{index:02d}/' if len(raw_chapters) > 1 else ''
                    progress = connection.execute('''SELECT
                        sum(CASE WHEN kind='segment' THEN 1 ELSE 0 END) total,
                        sum(CASE WHEN kind='segment' AND hash IS NOT NULL THEN 1 ELSE 0 END) done
                        FROM files WHERE task_id=? AND path LIKE ?''',
                        (row['id'], prefix + '%')).fetchone()
                    chapter_total, chapter_done = int(progress['total'] or 0), int(progress['done'] or 0)
                    local_m3u8 = chapter.get('m3u8_local')
                    local_vtt = chapter.get('vtt_local')
                    local_preview = chapter.get('preview_m3u8_local')
                    local_preview5 = chapter.get('preview5_m3u8_local')
                    completed = bool(chapter_total and chapter_done == chapter_total and local_m3u8)
                    chapters.append(dict(
                        index=index, title=chapter.get('title') or f'第 {index} 章',
                        duration=chapter.get('duration'),
                        status='completed' if completed else ('downloading' if chapter_done else 'pending'),
                        downloaded_segments=chapter_done, total_segments=chapter_total,
                        progress=100.0 if completed else round(chapter_done / chapter_total * 100, 2) if chapter_total else 0.0,
                        m3u8_url=self.media_url(row['source'], row['video_id'], local_m3u8) or chapter.get('m3u8_url'),
                        vtt_url=self.media_url(row['source'], row['video_id'], local_vtt) or chapter.get('vtt_url'),
                        preview_m3u8_file=local_preview,
                        preview_m3u8_url=(self.media_url(row['source'], row['video_id'], local_preview)
                                          or chapter.get('preview_m3u8_url')),
                        preview5_m3u8_file=local_preview5,
                        preview5_m3u8_url=(self.media_url(row['source'], row['video_id'], local_preview5)
                                           or chapter.get('preview5_m3u8_url'))))
            finally:
                if close_connection:
                    connection.close()
        playlist_local = metadata.get('m3u8_local') or 'index.m3u8'
        poster_file = metadata.get('poster_local') or row['poster_file']
        cover_file = metadata.get('cover_local') or poster_file
        preview_file = metadata.get('preview_image_local') or row['preview_file']
        vtt_file = metadata.get('vtt_local') or row['vtt_file']
        m3u8_file = metadata.get('m3u8_local')
        preview_m3u8_file = metadata.get('preview_m3u8_local') or row['preview_m3u8_file']
        preview5_m3u8_file = metadata.get('preview5_m3u8_local') or row['preview5_m3u8_file']
        return dict(task_id=row['id'],source=row['source'],video_id=row['video_id'],url=row['url'],status=row['status'],
                    r2_synced=bool(row['r2_synced']),
                    r2_sync_status=row['r2_sync_status'] or ('synced' if row['r2_synced'] else 'unsynced'),
                    r2_synced_at=row['r2_synced_at'], local_deleted=bool(row['local_deleted']),
                    local_deleted_at=row['local_deleted_at'],
                    downloaded_segments=done,total_segments=total,downloaded_bytes=row['downloaded_bytes'] or 0,
                    progress=100.0 if row['status']=='completed' else min(99.9,round(done/total*100,2)) if total else 0.0,
                    save_path=str(folder),playlist_path=str(folder/playlist_local) if row['status']=='completed' else None,
                    title=row['title'],poster_file=poster_file,
                    cover_url=self.media_url(row['source'], row['video_id'], cover_file) or row['cover_url'],
                    preview_file=preview_file,
                    preview_url=self.media_url(row['source'], row['video_id'], preview_file) or row['preview_url'],
                    vtt_file=vtt_file,
                    vtt_url=self.media_url(row['source'], row['video_id'], vtt_file) or row['vtt_url'],
                    m3u8_url=self.media_url(row['source'], row['video_id'], m3u8_file) or row['m3u8_url'],
                    preview_m3u8_file=preview_m3u8_file,
                    preview_m3u8_url=self.media_url(row['source'], row['video_id'], preview_m3u8_file) or row['preview_m3u8_url'],
                    preview5_m3u8_file=preview5_m3u8_file,
                    preview5_m3u8_url=self.media_url(row['source'], row['video_id'], preview5_m3u8_file) or row['preview5_m3u8_url'],
                    chapters=chapters,
                    message=row['message'],error=row['error'],warnings=json.loads(row['warnings']),
                    created_at=row['created'],updated_at=row['updated'])

    def get_many(self, task_ids, db=None):
        task_ids = list(dict.fromkeys(filter(None, task_ids)))
        if not task_ids:
            return {}
        if db is None:
            with self.db() as connection:
                return self.get_many(task_ids, connection)
        placeholders = ','.join('?' for _ in task_ids)
        rows = db.execute(f'SELECT * FROM tasks WHERE id IN ({placeholders})', task_ids).fetchall()
        return {row['id']: self.serialize_task(row, db) for row in rows}

    def get(self, task_id):
        return self.get_many([task_id]).get(task_id)

    def list(self, status=None, search=None, limit=100, offset=0):
        where, values = [], []
        if status:
            where.append('status=?'); values.append(status)
        if search:
            where.append('(title LIKE ? OR video_id LIKE ? OR url LIKE ?)')
            value = '%' + search + '%'
            values.extend((value, value, value))
        clause = (' WHERE ' + ' AND '.join(where)) if where else ''
        with self.db() as db:
            total = db.execute('SELECT count(*) FROM tasks' + clause, values).fetchone()[0]
            rows = db.execute('SELECT id FROM tasks' + clause + ' ORDER BY created DESC LIMIT ? OFFSET ?',
                              (*values, limit, offset)).fetchall()
        tasks = self.get_many([row['id'] for row in rows])
        return total, [tasks[row['id']] for row in rows if row['id'] in tasks]

    def exists(self, source, video_id):
        with self.db() as db:
            return db.execute('SELECT id FROM tasks WHERE source=? AND video_id=?',
                              (source, video_id)).fetchone() is not None

    def find(self, source, video_id):
        with self.db() as db:
            row = db.execute('SELECT id FROM tasks WHERE lower(trim(video_id))=lower(trim(?)) ORDER BY created LIMIT 1',
                             (video_id,)).fetchone()
        return self.get(row['id']) if row else None

    def deduplicate(self):
        rank = {'completed': 5, 'downloading': 4, 'queued': 3, 'failed': 2, 'deleting': 1}
        with self.db() as db:
            rows = [dict(row) for row in db.execute('SELECT id,video_id,status,updated FROM tasks ORDER BY created')]
        groups = {}
        for row in rows:
            groups.setdefault(row['video_id'].strip().lower(), []).append(row)
        duplicate_groups = [items for items in groups.values() if len(items) > 1]
        removed, kept = [], []
        for items in duplicate_groups:
            winner = max(items, key=lambda item: (rank.get(item['status'], 0), item['updated']))
            kept.append(winner['id'])
            for item in items:
                if item['id'] == winner['id']:
                    continue
                result = self.delete(item['id'])
                if result:
                    removed.append(item['id'])
        return dict(groups=len(duplicate_groups), removed=len(removed), kept_ids=kept, removed_ids=removed)

    def media_path(self, task_id, relative):
        task = self.get(task_id)
        if not task:
            return None
        folder = self.task_folder(task['source'], task['video_id'])
        target = (folder / relative).resolve()
        if target == folder or folder not in target.parents or not target.is_file():
            return None
        return target

    def state(self, task_id, status, error=None):
        with self.db() as db:
            self.rebuild_progress(db, task_id)
            message = error if error else {'queued':'等待下载','failed':'下载失败'}.get(status)
            db.execute('UPDATE tasks SET status=?,message=?,error=?,updated=? WHERE id=?',
                       (status,message,error,time.time(),task_id))

    def run(self, worker_index=0):
        while not self.stop_event.is_set():
            with self.active_condition:
                while (worker_index >= self.concurrency or not self.dispatch_enabled) and not self.stop_event.is_set():
                    self.active_condition.wait(1)
            if self.stop_event.is_set():
                break
            row = None
            with self.dispatch_lock:
                with self.db() as db:
                    row = db.execute("""SELECT * FROM tasks
                                        WHERE status='queued' OR assets_refresh IN (1,3)
                                        ORDER BY assets_refresh DESC,created LIMIT 1""").fetchone()
                    if row and row['status'] == 'queued':
                        db.execute("UPDATE tasks SET status='downloading',message='准备下载',updated=? WHERE id=?",
                                   (time.time(), row['id']))
                    elif row:
                        working_mode = 4 if row['assets_refresh'] == 3 else 2
                        db.execute('UPDATE tasks SET assets_refresh=?,updated=? WHERE id=?',
                                   (working_mode, time.time(), row['id']))
                if row:
                    with self.active_condition:
                        self.active_tasks.add(row['id'])
            if not row:
                self.wake.wait(1); self.wake.clear(); continue
            try:
                self.check_cancelled(row['id'])
                if row['status'] == 'completed' and row['assets_refresh']:
                    self.refresh_assets(dict(row), local_only=row['assets_refresh'] == 3)
                    continue
                for attempt in range(3):
                    try:
                        self.download(dict(row)); break
                    except Stopped:
                        raise
                    except Cancelled:
                        raise
                    except Exception:
                        if attempt == 2: raise
                        if self.stop_event.wait(2): raise Stopped()
            except Stopped:
                self.state(row['id'],'queued')
            except Cancelled:
                pass
            except Exception as exc:
                # Do not persist exception text containing signed URLs or credentials.
                message = str(exc) if isinstance(exc, DownloadError) else '下载失败：'+type(exc).__name__
                self.state(row['id'],'failed',message)
                log.warning('Download %s failed: %s',row['id'],message)
            finally:
                with self.active_condition:
                    self.active_tasks.discard(row['id'])
                    self.active_condition.notify_all()
                self.wake.set()

    @contextmanager
    def task_source(self, task):
        metadata = json.loads(task.get('metadata_json') or '{}')
        if metadata.get('_imported'):
            with requests.Session() as session:
                transport = HttpTransport(session, metadata.get('url'))
                yield metadata, SigningTransport(transport, self.signers, task) if self.signers else transport
        else:
            with self.resolver(task['url']) as (metadata, transport):
                yield metadata, SigningTransport(transport, self.signers, task) if self.signers else transport

    def refresh_assets(self, task, local_only=False):
        folder = self.task_folder(task['source'], task['video_id'])
        try:
            self.check_cancelled(task['id'])
            if local_only:
                metadata = json.loads(task.get('metadata_json') or '{}')
                warnings = json.loads(task.get('warnings') or '[]')
                warnings = self.generate_import_previews(task, folder, metadata, warnings)
            else:
                with self.task_source(task) as (metadata, transport):
                    self.check_cancelled(task['id'])
                    metadata = self.merge_existing_metadata(task, metadata)
                    warnings = self.assets(task, folder, metadata, transport)
                    warnings = self.generate_import_previews(task, folder, metadata, warnings)
            with self.db() as db:
                self.save_metadata(db, task['id'], metadata)
                db.execute("UPDATE tasks SET assets_refresh=0,message='下载完成',warnings=?,updated=? WHERE id=?",
                           (json.dumps(warnings,ensure_ascii=False),time.time(),task['id']))
        except Cancelled:
            raise
        except Exception as exc:
            with self.db() as db:
                warnings = json.loads(db.execute('SELECT warnings FROM tasks WHERE id=?',(task['id'],)).fetchone()['warnings'])
                if '附加资源补全失败' not in warnings:
                    warnings.append('附加资源补全失败')
                db.execute('UPDATE tasks SET assets_refresh=0,warnings=?,updated=? WHERE id=?',
                           (json.dumps(warnings,ensure_ascii=False),time.time(),task['id']))
            log.warning('Asset refresh %s failed: %s',task['id'],type(exc).__name__)

    def merge_existing_metadata(self, task, metadata):
        try:
            previous = json.loads(task.get('metadata_json') or '{}')
        except (ValueError, TypeError):
            return metadata
        local_fields = {'cover': 'cover_local', 'poster': 'poster_local',
                        'preview_image': 'preview_image_local', 'vtt_url': 'vtt_local',
                        'preview_m3u8_url': 'preview_m3u8_local',
                        'preview5_m3u8_url': 'preview5_m3u8_local'}
        for field, local in local_fields.items():
            if not metadata.get(field) and previous.get(field):
                metadata[field] = previous[field]
            if previous.get(local):
                metadata.setdefault(local,previous[local])
        if previous.get('m3u8_local'):
            metadata.setdefault('m3u8_local', previous['m3u8_local'])
        previous_chapters = previous.get('chapters') if isinstance(previous.get('chapters'), list) else []
        current_chapters = metadata.get('chapters') if isinstance(metadata.get('chapters'), list) else []
        if previous_chapters and not current_chapters:
            metadata['chapters'] = previous_chapters
        elif previous_chapters and current_chapters:
            chapter_local_fields = ('m3u8_local', 'vtt_local', 'preview_m3u8_local',
                                    'preview5_m3u8_local')
            for index, chapter in enumerate(current_chapters):
                if not isinstance(chapter, dict) or index >= len(previous_chapters):
                    continue
                previous_chapter = previous_chapters[index]
                if not isinstance(previous_chapter, dict):
                    continue
                for field in chapter_local_fields:
                    if previous_chapter.get(field):
                        chapter.setdefault(field, previous_chapter[field])
        return metadata

    def task_message(self, task_id, message):
        self.check_cancelled(task_id)
        with self.db() as db:
            db.execute('UPDATE tasks SET message=?,updated=? WHERE id=?',
                       (message, time.time(), task_id))

    @staticmethod
    def remove_asset_warnings(warnings, *fields):
        prefixes = tuple(field + ' ' for field in fields)
        warnings[:] = [warning for warning in warnings if not warning.startswith(prefixes)]

    def record_generated_assets(self, task_id, folder):
        paths = set()
        for pattern in ('cover.jpg', 'poster.jpg', 'preview_image.mp4', 'preview.vtt',
                        'preview.jpg', 'preview-*.jpg', 'preview.m3u8', 'preview-track-*.m3u8',
                        'preview5.m3u8', 'preview5-track-*.m3u8'):
            paths.update(path for path in folder.rglob(pattern) if path.is_file())
        with self.db() as db:
            for path in paths:
                checksum = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        checksum.update(chunk)
                db.execute('INSERT OR REPLACE INTO files(task_id,path,kind,size,hash) VALUES(?,?,?,?,?)',
                           (task_id, path.relative_to(folder).as_posix(), 'asset',
                            path.stat().st_size, checksum.hexdigest()))

    def generate_import_previews(self, task, folder, metadata, warnings):
        """Generate short playlists and fill missing assets for imported videos."""
        imported = bool(metadata.get('_imported'))
        first_local = metadata.get('m3u8_local') or 'index.m3u8'
        source = folder / first_local
        if not source.is_file():
            return warnings
        need_cover = imported and not metadata.get('cover_local')
        need_poster = imported and not metadata.get('poster_local')
        need_hover = imported and not metadata.get('preview_image_local')
        need_vtt = imported and not metadata.get('vtt_local')
        duration = None
        if need_cover or need_poster or need_hover or need_vtt:
            duration = self.preview_generator.duration(source)

        if need_cover or need_poster:
            self.task_message(task['id'], '正在生成本地封面')
            try:
                self.preview_generator.cover(source, folder, duration, need_cover, need_poster)
            except Exception:
                log.warning('Cover generation %s failed', task['id'], exc_info=True)
            if need_cover and (folder / 'cover.jpg').is_file():
                metadata['cover_local'] = 'cover.jpg'
                self.remove_asset_warnings(warnings, 'cover')
            elif need_cover:
                warnings.append('cover 本地生成失败')
            if need_poster and (folder / 'poster.jpg').is_file():
                metadata['poster_local'] = 'poster.jpg'
                self.remove_asset_warnings(warnings, 'poster')
            elif need_poster:
                warnings.append('poster 本地生成失败')

        if need_hover:
            self.task_message(task['id'], '正在生成本地悬浮预览')
            try:
                generated = self.preview_generator.hover(source, folder, duration)
            except Exception:
                generated = False
                log.warning('Hover preview generation %s failed', task['id'], exc_info=True)
            if generated:
                metadata['preview_image_local'] = 'preview_image.mp4'
                self.remove_asset_warnings(warnings, 'preview_image')
            else:
                warnings.append('preview_image 本地生成失败')

        if need_vtt:
            self.task_message(task['id'], '正在生成本地进度预览')
            try:
                generated = self.preview_generator.vtt(source, folder, duration)
            except Exception:
                generated = False
                log.warning('VTT generation %s failed', task['id'], exc_info=True)
            if generated:
                metadata['vtt_local'] = 'preview.vtt'
                warnings[:] = [warning for warning in warnings
                               if warning not in ('VTT 来源未提供', 'VTT 或进度缩略图下载失败')
                               and not warning.startswith('第 1 章：VTT ')]
            else:
                warnings.append('VTT 本地生成失败')

        raw_chapters = metadata.get('chapters') if isinstance(metadata.get('chapters'), list) else []
        preview_targets = []
        if raw_chapters:
            for index, chapter in enumerate(raw_chapters, 1):
                if not isinstance(chapter, dict) or not chapter.get('m3u8_local'):
                    continue
                chapter_source = folder / chapter['m3u8_local']
                if chapter_source.is_file():
                    preview_targets.append((index, chapter, chapter_source))
        if not preview_targets:
            preview_targets.append((None, metadata, source))

        previews = ((180, 'preview.m3u8', 'preview_m3u8_local', '3'),
                    (300, 'preview5.m3u8', 'preview5_m3u8_local', '5'))
        for chapter_number, target, chapter_source in preview_targets:
            chapter_label = f'第 {chapter_number} 章' if chapter_number else None
            for seconds, name, metadata_field, minutes in previews:
                message = (f'正在生成{chapter_label}{minutes}分钟预览清单'
                           if chapter_label else f'正在生成{minutes}分钟预览清单')
                self.task_message(task['id'], message)
                warning = (f'{chapter_label}：{name} 本地生成失败'
                           if chapter_label else name + ' 本地生成失败')
                try:
                    generated = self.preview_generator.preview_playlist(
                        chapter_source.parent, seconds, name)
                except Exception:
                    generated = False
                    log.warning('%s generation %s chapter=%s failed',
                                name, task['id'], chapter_number, exc_info=True)
                if generated:
                    local_path = (chapter_source.parent / name).relative_to(folder).as_posix()
                    target[metadata_field] = local_path
                    if chapter_number in (None, 1):
                        metadata[metadata_field] = local_path
                    warnings[:] = [item for item in warnings if item != warning]
                elif warning not in warnings:
                    warnings.append(warning)
        try:
            self.record_generated_assets(task['id'], folder)
        except Exception:
            warnings.append('本地预览文件登记失败')
            log.warning('Preview asset registration %s failed', task['id'], exc_info=True)
        return list(dict.fromkeys(warnings))

    @staticmethod
    def prefix_plan(plan, prefix):
        if not prefix:
            return plan
        prefix = prefix.rstrip('/') + '/'
        for resource in plan['resources']:
            resource['path'] = prefix + resource['path']
            if resource.get('key'):
                resource['key'] = prefix + resource['key']
        plan['manifests'] = {prefix + name: content for name, content in plan['manifests'].items()}
        return plan

    def download_plan(self, task, folder, metadata, transport, stream_url,
                      chapter_number, chapter_count, fingerprints):
        plan = build_plan(self.file_downloader, task['id'], transport, stream_url)
        prefix = f'{chapter_number:02d}' if chapter_count > 1 else ''
        plan = self.prefix_plan(plan, prefix)
        key = str(chapter_number)
        with self.db() as db:
            old_fingerprint = fingerprints.get(key)
            if old_fingerprint and old_fingerprint != plan['fingerprint']:
                raise DownloadError(f'第 {chapter_number} 章分片结构已变化，保留原文件，禁止混合不同版本')
            fingerprints[key] = plan['fingerprint']
            db.execute('UPDATE tasks SET plan=?,updated=? WHERE id=?',
                       (json.dumps({'chapters': fingerprints}), time.time(), task['id']))
            for resource in plan['resources']:
                db.execute('INSERT OR IGNORE INTO files(task_id,path,kind) VALUES(?,?,?)',
                           (task['id'], resource['path'], resource['kind']))
            self.save_metadata(db, task['id'], metadata)
            self.rebuild_progress(db, task['id'])
            db.execute('UPDATE tasks SET message=?,updated=? WHERE id=?',
                       (f'正在下载第 {chapter_number} / {chapter_count} 章', time.time(), task['id']))
        pending = []
        for resource in plan['resources']:
            self.check_cancelled(task['id'])
            path = folder / resource['path']
            with self.db() as db:
                saved = db.execute('SELECT * FROM files WHERE task_id=? AND path=?',
                                   (task['id'], resource['path'])).fetchone()
            if (saved['hash'] and path.is_file() and path.stat().st_size == saved['size']
                    and digest(path.read_bytes()) == saved['hash']):
                if resource['kind'] == 'key':
                    current_key = self.file_downloader.fetch(
                        task['id'], transport, resource['url'], limit=1024, resource_type='key')
                    if digest(current_key) != saved['hash']:
                        raise DownloadError('来源密钥已变化，禁止混合不同版本')
                continue
            with self.db() as db:
                db.execute('UPDATE files SET hash=NULL,size=0 WHERE task_id=? AND path=?',
                           (task['id'], resource['path']))
            pending.append(resource)

        def download_resource(resource, current_transport):
            if self.stop_event.is_set():
                raise Stopped()
            self.check_cancelled(task['id'])
            result = self.file_downloader.download(
                task['id'], current_transport, resource['url'], folder / resource['path'],
                byte_range=resource['byte_range'],
                validator=lambda data, current=resource: validate_payload(
                    data, current, folder),
                resource_type=resource['kind'])
            return resource, result

        def save_result(resource, result):
            with self.db() as db:
                db.execute('UPDATE files SET size=?,hash=? WHERE task_id=? AND path=?',
                           (result['size'], result['hash'], task['id'], resource['path']))

        # Keys and initialization files must exist before encrypted segments are
        # validated. They are few and remain sequential.
        for resource in (item for item in pending if item['kind'] != 'segment'):
            current, result = download_resource(resource, transport)
            save_result(current, result)

        segments = [item for item in pending if item['kind'] == 'segment']
        completed_since_flush, last_flush = 0, time.monotonic()
        if segments:
            factory = transport.concurrent_factory()
            worker_state = threading.local()
            def download_segment(resource):
                current_transport = getattr(worker_state, 'transport', None)
                if current_transport is None:
                    current_transport = factory()
                    worker_state.transport = current_transport
                return download_resource(resource, current_transport)
            workers = min(self.segment_concurrency, len(segments))
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix='ts-download') as executor:
                futures = [executor.submit(download_segment, resource)
                           for resource in segments]
                for future in as_completed(futures):
                    current, result = future.result()
                    save_result(current, result)
                    completed_since_flush += 1
                    if (completed_since_flush >= 10
                            or time.monotonic() - last_flush >= 5):
                        with self.db() as db:
                            self.rebuild_progress(db, task['id'])
                            progress = db.execute(
                                '''SELECT downloaded_segments,total_segments FROM tasks
                                   WHERE id=?''', (task['id'],)).fetchone()
                            db.execute('UPDATE tasks SET message=?,updated=? WHERE id=?',
                                       (f"第 {chapter_number} / {chapter_count} 章 · 已下载 "
                                        f"{progress['downloaded_segments']} / "
                                        f"{progress['total_segments']} 分片",
                                        time.time(), task['id']))
                        completed_since_flush, last_flush = 0, time.monotonic()
        for name, content in plan['manifests'].items():
            self.file_downloader.save(folder / name, content.encode())
        with self.db() as db:
            self.rebuild_progress(db, task['id'])
        return (f'{prefix}/' if prefix else '') + 'index.m3u8'

    def download(self, task):
        folder = self.task_folder(task['source'], task['video_id'])
        folder.mkdir(parents=True, exist_ok=True)
        with self.task_source(task) as (metadata, transport):
            chapters = metadata.get('chapters') if isinstance(metadata.get('chapters'), list) else []
            if not chapters:
                chapters = [dict(index=1, title='第 1 章', duration=metadata.get('duration'),
                                 m3u8_url=metadata.get('m3u8_url'), vtt_url=metadata.get('vtt_url'))]
                persist_chapters = False
            else:
                persist_chapters = True
            with self.db() as db:
                previous_raw = db.execute('SELECT plan FROM tasks WHERE id=?', (task['id'],)).fetchone()['plan']
            try:
                previous_plan = json.loads(previous_raw) if previous_raw else {}
            except (ValueError, TypeError):
                previous_plan = {}
            fingerprints = dict(previous_plan.get('chapters') or {})
            if not fingerprints and len(chapters) == 1 and previous_plan.get('fingerprint'):
                fingerprints['1'] = previous_plan['fingerprint']
            warnings = []
            for number, chapter in enumerate(chapters, 1):
                self.check_cancelled(task['id'])
                chapter['index'] = number
                if chapter.get('_player_url'):
                    if number > 1 or not chapter.get('m3u8_url'):
                        chapter.update(transport.open_123av_player(chapter['_player_url']))
                stream_url = chapter.get('m3u8_url')
                if not stream_url:
                    raise DownloadError(f'第 {number} 章没有播放地址')
                local_m3u8 = self.download_plan(task, folder, metadata, transport, stream_url,
                                                 number, len(chapters), fingerprints)
                chapter['m3u8_local'] = local_m3u8
                chapter_folder = folder / f'{number:02d}' if len(chapters) > 1 else folder
                chapter_warnings = self.assets(task, chapter_folder, chapter, transport,
                                               asset_fields=(), download_vtt=True)
                warnings.extend(f'第 {number} 章：{warning}' for warning in chapter_warnings)
                with self.db() as db:
                    self.save_metadata(db, task['id'], metadata)
            first = chapters[0]
            metadata['m3u8_local'] = first['m3u8_local']
            metadata['vtt_local'] = first.get('vtt_local')
            metadata['m3u8_url'] = first.get('m3u8_url')
            metadata['vtt_url'] = first.get('vtt_url')
            if persist_chapters:
                metadata['chapters'] = chapters
            else:
                metadata.pop('chapters', None)
            with self.db() as db:
                db.execute("UPDATE tasks SET message='正在下载封面和预览',updated=? WHERE id=?",
                           (time.time(), task['id']))
            warnings.extend(self.assets(task, folder, metadata, transport, download_vtt=False))
            warnings = self.generate_import_previews(task, folder, metadata, warnings)
            if persist_chapters and metadata.get('vtt_local') and not first.get('vtt_local'):
                first['vtt_local'] = metadata['vtt_local']
                metadata['chapters'] = chapters
            with self.db() as db:
                self.save_metadata(db, task['id'], metadata)
                self.rebuild_progress(db, task['id'])
                db.execute("UPDATE tasks SET status='completed',message='下载完成',error=NULL,warnings=?,updated=? WHERE id=?",
                           (json.dumps(list(dict.fromkeys(warnings)), ensure_ascii=False), time.time(), task['id']))
            self.notify_completed(task, metadata, folder)

    def assets(self, task, folder, metadata, transport,
               asset_fields=('cover', 'poster', 'preview_image'), download_vtt=True):
        warnings = []
        task_root = self.task_folder(task['source'], task['video_id'])
        relative_folder = folder.resolve().relative_to(task_root.resolve())
        def database_path(name):
            return (relative_folder / name).as_posix() if relative_folder.parts else name
        def save(url, name, image=False, visual=False, resource_type='asset'):
            stem = str(Path(name).with_suffix('')) if image or visual else name
            database_stem = database_path(stem)
            with self.db() as db:
                if image or visual:
                    records = db.execute("SELECT * FROM files WHERE task_id=? AND path LIKE ? AND kind='asset'",
                                         (task['id'],database_stem+'.%')).fetchall()
                else:
                    record = db.execute('SELECT * FROM files WHERE task_id=? AND path=?',
                                        (task['id'],database_path(name))).fetchone()
                    records = [record] if record else []
            for record in records:
                path = task_root / record['path']
                if record['hash'] and path.is_file() and digest(path.read_bytes()) == record['hash']:
                    return path.read_bytes(), record['path']
            def validate(data):
                if not data or data.lstrip().lower().startswith((b'<html',b'<!doctype')):
                    raise DownloadError('附加资源返回空内容或错误页')
                extension = asset_extension(data)
                if image and extension not in ('.jpg','.png','.gif','.webp'):
                    raise DownloadError('图片资源不是支持的图片格式')
                if visual and extension not in ('.jpg','.png','.gif','.webp','.mp4','.webm'):
                    raise DownloadError('预览资源不是支持的图片或视频格式')
            data = self.file_downloader.fetch(task['id'], transport, url,
                                              limit=20*1024*1024, validator=validate,
                                              resource_type=resource_type)
            extension = asset_extension(data)
            if image or visual:
                name = stem + extension
            path = folder / name
            result = self.file_downloader.save(path,data)
            with self.db() as db:
                db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)',
                           (task['id'],database_path(name),'asset',result['size'],result['hash']))
            return data, database_path(name)
        for field in asset_fields:
            self.check_cancelled(task['id'])
            url = metadata.get(field)
            if not url:
                warnings.append(field+' 来源未提供')
                continue
            try:
                _, local_name = save(url,field+'.asset',image=field != 'preview_image',
                                     visual=field == 'preview_image', resource_type=field)
                metadata[field+'_local'] = local_name
            except (Stopped, Cancelled):
                raise
            except Exception:
                warnings.append(field+' 下载失败')
        if download_vtt and metadata.get('vtt_url'):
            try:
                self.check_cancelled(task['id'])
                base = metadata['vtt_url']
                raw = save(base,'preview-source.vtt',resource_type='vtt')[0].decode('utf-8-sig')
                if not raw.startswith('WEBVTT'): raise DownloadError('VTT 格式错误')
                lines, images = raw.splitlines(), {}
                for i,line in enumerate(lines):
                    self.check_cancelled(task['id'])
                    if '-->' not in line: continue
                    if i+1 >= len(lines): raise DownloadError('VTT 索引不完整')
                    reference = lines[i+1].strip()
                    remote = urljoin(base,reference.split('#',1)[0])
                    if remote not in images:
                        number = len(images) + 1
                        # Keep every VTT sprite beside the video, with names that
                        # cannot collide with the ordinary preview image.
                        stem = f'vtt-preview-{number:03d}'
                        _, name = save(remote,stem+'.asset',image=True,resource_type='vtt_image')
                        images[remote]=Path(name).name
                    lines[i+1] = images[remote] + ('#'+reference.split('#',1)[1] if '#' in reference else '')
                self.file_downloader.save(folder/'preview.vtt','\n'.join(lines).encode())
                metadata['vtt_local'] = database_path('preview.vtt')
                # Migration from the previous layout happens only after the new
                # VTT and root-level preview files have been written successfully.
                old_folder = (folder/'thumbnails').resolve()
                if old_folder.exists() and folder.resolve() in old_folder.parents:
                    shutil.rmtree(old_folder)
                with self.db() as db:
                    db.execute("DELETE FROM files WHERE task_id=? AND path LIKE 'thumbnails/%'",(task['id'],))
            except (Stopped, Cancelled):
                raise
            except Exception:
                warnings.append('VTT 或进度缩略图下载失败')
        elif download_vtt:
            warnings.append('VTT 来源未提供')
        return warnings
