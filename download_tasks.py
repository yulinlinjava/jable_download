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
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from urllib.parse import urljoin, urlsplit

import m3u8
import requests
from Crypto.Cipher import AES
from bs4 import BeautifulSoup
from config import headers

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
              limit=64 * 1024 * 1024, attempts=3, validator=None):
        last_error = None
        for attempt in range(attempts):
            if self.stop_event.is_set():
                raise Stopped()
            self.cancel_check(task_id)
            try:
                data = transport.get(url, byte_range=byte_range, limit=limit)
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
                 limit=64 * 1024 * 1024, attempts=3, validator=None):
        data = self.fetch(task_id, transport, url, byte_range=byte_range,
                          limit=limit, attempts=attempts, validator=validator)
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


class BrowserTransport:
    """Fetch with the same browser origin/session that resolved the stream."""
    def __init__(self, driver, cdp_fallback=False):
        self.driver = driver
        self.cdp_fallback = cdp_fallback

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

    def get(self, url, byte_range=None, limit=64 * 1024 * 1024):
        public_url(url)
        result = self.driver.execute_async_script("""
          const [url, range, limit, done] = arguments;
          const controller = new AbortController();
          const timer = setTimeout(()=>controller.abort(), 30000);
          (async()=>{
            const r = await fetch(url, {redirect:'error', signal:controller.signal,
              headers:range ? {Range:'bytes='+range[0]+'-'+(range[0]+range[1]-1)} : {}});
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
        """, url, byte_range, limit)
        if not result or 'error' in result:
            if byte_range is None:
                return self.cdp_get(url,limit)
            raise DownloadError('资源获取失败：' + ((result or {}).get('error', '浏览器无响应')))
        data = base64.b64decode(result['data'])
        if byte_range and len(data) != byte_range[1]:
            raise DownloadError('分片字节范围长度不匹配')
        return data


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
            players = api.provider_123av.player_urls(source)
            if not players:
                raise DownloadError('未找到播放器')
            p = urlsplit(players[0])
            if p.scheme != 'https' or p.hostname != 'javplayer.cc' or p.username or p.password or p.port not in (None,443) or not re.fullmatch(r'/e/[A-Za-z0-9]+', p.path):
                raise DownloadError('播放器地址不支持')
            target = 'https://javplayer.cc' + p.path
        else:
            target, metadata = url, None
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
            if urlsplit(browser.current_url).hostname != 'javplayer.cc':
                raise DownloadError('播放器跳转到不支持的地址')
            resources = browser.execute_script("return performance.getEntriesByType('resource').map(x=>x.name)")
            metadata['vtt_url'] = next((u for u in resources if urlsplit(u).path.endswith('.vtt')), None)
            metadata['m3u8_url'] = next((u for u in resources if urlsplit(u).path.endswith('.m3u8')), None)
            return metadata['m3u8_url']
        WebDriverWait(driver, 30).until(loaded)
        if api.provider(url) == 'jable':
            enrich_jable_card(metadata)
        yield metadata, BrowserTransport(driver, cdp_fallback=api.provider(url) == 'jable')
    finally:
        driver.quit()


def build_plan(file_downloader, task_id, transport, stream_url):
    """Pick the highest resolution and preserve keys, maps and byte ranges."""
    def playlist(url):
        def validate(data):
            if not data.decode('utf-8-sig').lstrip().startswith('#EXTM3U'):
                raise DownloadError('来源没有返回有效 m3u8')
        raw = file_downloader.fetch(task_id, transport, url, limit=4 * 1024 * 1024,
                                    validator=validate).decode('utf-8-sig')
        return m3u8.loads(raw)

    master_url = stream_url
    top = playlist(stream_url)
    tracks = [('video', stream_url, top)]
    master = None
    if top.is_variant:
        if not top.playlists:
            raise DownloadError('主清单没有视频流')
        chosen = max(top.playlists, key=lambda p: ((p.stream_info.resolution or (0,0))[0] * (p.stream_info.resolution or (0,0))[1], p.stream_info.bandwidth or 0))
        stream_url = urljoin(stream_url, chosen.uri)
        tracks = [('video', stream_url, playlist(stream_url))]
        audio = [a for a in top.media if a.type == 'AUDIO' and a.group_id == chosen.stream_info.audio and a.uri]
        if audio:
            selected = next((a for a in audio if a.default == 'YES'), audio[0])
            audio_url = urljoin(master_url, selected.uri)
            tracks.append(('audio', audio_url, playlist(audio_url)))
        master = chosen.stream_info

    resources, manifests = [], {}
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
                    path = f'segments/{track}-init-{len(map_paths):04d}.ts'
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
            resources.append(dict(path=f'segments/{track}-{i+1:06d}.ts', kind='segment', url=original,
                                  byte_range=byte_range, duration=segment.duration,
                                  sequence=int(data.media_sequence or 0)+i, key=key_id,
                                  iv=segment.key.iv if key_id else None,
                                  discontinuity=bool(segment.discontinuity)))
        # Mutate only after collecting original URLs (key/map objects are shared).
        for i, segment in enumerate(data.segments):
            segment.uri = f'segments/{track}-{i+1:06d}.ts'
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

    def __init__(self, root, resolver=browser_source, start_enabled=True, database=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = Path(database).resolve() if database else self.root / 'tasks.sqlite3'
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.resolver = resolver
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
                  m3u8_url TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
                  status TEXT NOT NULL DEFAULT 'pending', message TEXT, error TEXT,
                  warnings TEXT NOT NULL DEFAULT '[]', plan TEXT,
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
            ''')
            db.execute("UPDATE tasks SET status='queued',updated=? WHERE status='downloading'", (time.time(),))
            db.execute('UPDATE tasks SET assets_refresh=1 WHERE assets_refresh=2')
            self.rebuild_progress(db)
        self.speed_meter = SpeedMeter(window_seconds=5.0)
        self.file_downloader = FileDownloader(self.speed_meter, self.stop_event, self.check_cancelled)
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

    def set_scheduler(self, enabled, concurrency):
        value = max(1, min(self.MAX_CONCURRENCY, int(concurrency)))
        with self.active_condition:
            self.dispatch_enabled = bool(enabled)
            self.concurrency = value
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

    def is_cancelled(self, task_id):
        with self.active_condition:
            return task_id in self.cancelled

    def check_cancelled(self, task_id):
        if self.is_cancelled(task_id):
            raise Cancelled()

    def delete(self, task_id):
        with self.dispatch_lock:
            with self.db() as db:
                row = db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
                if not row:
                    return None
                db.execute("UPDATE tasks SET status='deleting',assets_refresh=0,updated=? WHERE id=?",
                           (time.time(),task_id))
            with self.active_condition:
                self.cancelled.add(task_id)
        self.wake.set()
        deadline = time.monotonic() + 45
        with self.active_condition:
            while task_id in self.active_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DownloadError('任务正在停止，请稍后重试删除')
                self.active_condition.wait(min(1,remaining))
        folder = (self.root / row['source'] / row['video_id']).resolve()
        if folder == self.root or self.root not in folder.parents:
            raise DownloadError('任务资源目录不安全，拒绝删除')
        if folder.exists():
            shutil.rmtree(folder)
        with self.db() as db:
            db.execute('DELETE FROM files WHERE task_id=?',(task_id,))
            db.execute('DELETE FROM tasks WHERE id=?',(task_id,))
        with self.active_condition:
            self.cancelled.discard(task_id)
        return dict(task_id=task_id,deleted=True,deleted_path=str(folder))

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

    @staticmethod
    def save_metadata(db, task_id, metadata):
        db.execute('''UPDATE tasks SET title=coalesce(?,title),release_date=coalesce(?,release_date),
                      duration=coalesce(?,duration),cover_url=coalesce(?,cover_url),
                      poster_file=coalesce(?,poster_file),preview_url=coalesce(?,preview_url),
                      preview_file=coalesce(?,preview_file),vtt_url=coalesce(?,vtt_url),
                      vtt_file=coalesce(?,vtt_file),m3u8_url=coalesce(?,m3u8_url),metadata_json=? WHERE id=?''',
                   (metadata.get('title'), metadata.get('release_date') or metadata.get('published_at'),
                    metadata.get('duration'), metadata.get('cover'),
                    metadata.get('poster_local') or metadata.get('cover_local'), metadata.get('preview_image'),
                    metadata.get('preview_image_local'), metadata.get('vtt_url'), metadata.get('vtt_local'),
                    metadata.get('m3u8_url'), json.dumps(metadata,ensure_ascii=False), task_id))

    def serialize_task(self, row):
        total, done = row['total_segments'] or 0, row['downloaded_segments'] or 0
        folder = self.root / row['source'] / row['video_id']
        return dict(task_id=row['id'],source=row['source'],video_id=row['video_id'],url=row['url'],status=row['status'],
                    downloaded_segments=done,total_segments=total,downloaded_bytes=row['downloaded_bytes'] or 0,
                    progress=100.0 if row['status']=='completed' else min(99.9,round(done/total*100,2)) if total else 0.0,
                    save_path=str(folder),playlist_path=str(folder/'index.m3u8') if row['status']=='completed' else None,
                    title=row['title'],poster_file=row['poster_file'],cover_url=row['cover_url'],
                    preview_file=row['preview_file'],preview_url=row['preview_url'],vtt_file=row['vtt_file'],
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
        return {row['id']: self.serialize_task(row) for row in rows}

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
        folder = (self.root / task['source'] / task['video_id']).resolve()
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
                    row = db.execute("SELECT * FROM tasks WHERE status='queued' OR assets_refresh=1 ORDER BY assets_refresh DESC,created LIMIT 1").fetchone()
                    if row and row['status'] == 'queued':
                        db.execute("UPDATE tasks SET status='downloading',message='准备下载',updated=? WHERE id=?",
                                   (time.time(), row['id']))
                    elif row:
                        db.execute('UPDATE tasks SET assets_refresh=2,updated=? WHERE id=?', (time.time(), row['id']))
                if row:
                    with self.active_condition:
                        self.active_tasks.add(row['id'])
            if not row:
                self.wake.wait(1); self.wake.clear(); continue
            try:
                self.check_cancelled(row['id'])
                if row['status'] == 'completed' and row['assets_refresh']:
                    self.refresh_assets(dict(row))
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

    def refresh_assets(self, task):
        folder = self.root / task['source'] / task['video_id']
        try:
            self.check_cancelled(task['id'])
            with self.resolver(task['url']) as (metadata, transport):
                self.check_cancelled(task['id'])
                metadata = self.merge_existing_metadata(task, metadata)
                warnings = self.assets(task, folder, metadata, transport)
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
        for field in ('cover','poster','preview_image','vtt_url'):
            if not metadata.get(field) and previous.get(field):
                metadata[field] = previous[field]
            local = field + '_local' if field != 'vtt_url' else 'vtt_local'
            if previous.get(local):
                metadata.setdefault(local,previous[local])
        return metadata

    def download(self, task):
        folder = self.root / task['source'] / task['video_id']
        folder.mkdir(parents=True,exist_ok=True)
        with self.resolver(task['url']) as (metadata, transport):
            plan = build_plan(self.file_downloader, task['id'], transport, metadata['m3u8_url'])
            with self.db() as db:
                previous = db.execute('SELECT plan FROM tasks WHERE id=?',(task['id'],)).fetchone()['plan']
                if previous and json.loads(previous)['fingerprint'] != plan['fingerprint']:
                    raise DownloadError('来源分片结构已变化，保留原文件，禁止混合不同版本')
                db.execute('UPDATE tasks SET plan=?,updated=? WHERE id=?',(json.dumps(plan),time.time(),task['id']))
                for resource in plan['resources']:
                    db.execute('INSERT OR IGNORE INTO files(task_id,path,kind) VALUES(?,?,?)',(task['id'],resource['path'],resource['kind']))
                self.save_metadata(db, task['id'], metadata)
                self.rebuild_progress(db, task['id'])
                db.execute("UPDATE tasks SET message='正在下载分片',updated=? WHERE id=?", (time.time(),task['id']))
            completed_since_flush, last_flush = 0, time.monotonic()
            # Validate hashes once per resume. File existence alone is not completion.
            for resource in plan['resources']:
                self.check_cancelled(task['id'])
                path = folder / resource['path']
                with self.db() as db:
                    saved = db.execute('SELECT * FROM files WHERE task_id=? AND path=?',(task['id'],resource['path'])).fetchone()
                if saved['hash'] and path.is_file() and path.stat().st_size == saved['size'] and digest(path.read_bytes()) == saved['hash']:
                    if resource['kind'] == 'key':
                        current_key = self.file_downloader.fetch(
                            task['id'], transport, resource['url'], limit=1024)
                        if digest(current_key) != saved['hash']:
                            raise DownloadError('来源密钥已变化，禁止混合不同版本')
                    continue
                with self.db() as db:
                    db.execute('UPDATE files SET hash=NULL,size=0 WHERE task_id=? AND path=?',(task['id'],resource['path']))
                if self.stop_event.is_set(): raise Stopped()
                self.check_cancelled(task['id'])
                result = self.file_downloader.download(
                    task['id'], transport, resource['url'], path,
                    byte_range=resource['byte_range'],
                    validator=lambda data, current=resource: validate_payload(data,current,folder))
                with self.db() as db:
                    db.execute('UPDATE files SET size=?,hash=? WHERE task_id=? AND path=?',
                               (result['size'],result['hash'],task['id'],resource['path']))
                if resource['kind'] == 'segment':
                    completed_since_flush += 1
                    if completed_since_flush >= 10 or time.monotonic() - last_flush >= 5:
                        with self.db() as db:
                            self.rebuild_progress(db, task['id'])
                            progress = db.execute('''SELECT downloaded_segments,total_segments FROM tasks
                                                     WHERE id=?''',(task['id'],)).fetchone()
                            db.execute('UPDATE tasks SET message=?,updated=? WHERE id=?',
                                       (f"已下载 {progress['downloaded_segments']} / {progress['total_segments']} 分片",
                                        time.time(),task['id']))
                        completed_since_flush, last_flush = 0, time.monotonic()
            with self.db() as db:
                self.rebuild_progress(db, task['id'])
                db.execute("UPDATE tasks SET message='正在下载封面和预览',updated=? WHERE id=?",
                           (time.time(),task['id']))
            warnings = self.assets(task,folder,metadata,transport)
            for name, content in plan['manifests'].items():
                self.file_downloader.save(folder/name,content.encode())
            with self.db() as db:
                self.save_metadata(db, task['id'], metadata)
                self.rebuild_progress(db, task['id'])
                db.execute("UPDATE tasks SET status='completed',message='下载完成',error=NULL,warnings=?,updated=? WHERE id=?",
                           (json.dumps(warnings,ensure_ascii=False),time.time(),task['id']))

    def assets(self, task, folder, metadata, transport):
        warnings = []
        def save(url, name, image=False, visual=False):
            stem = str(Path(name).with_suffix('')) if image or visual else name
            with self.db() as db:
                if image or visual:
                    records = db.execute("SELECT * FROM files WHERE task_id=? AND path LIKE ? AND kind='asset'",
                                         (task['id'],stem+'.%')).fetchall()
                else:
                    record = db.execute('SELECT * FROM files WHERE task_id=? AND path=?',(task['id'],name)).fetchone()
                    records = [record] if record else []
            for record in records:
                path = folder / record['path']
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
                                              limit=20*1024*1024, validator=validate)
            extension = asset_extension(data)
            if image or visual:
                name = stem + extension
            path = folder / name
            result = self.file_downloader.save(path,data)
            with self.db() as db:
                db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)',
                           (task['id'],name,'asset',result['size'],result['hash']))
            return data, name
        for field in ('cover','poster','preview_image'):
            self.check_cancelled(task['id'])
            url = metadata.get(field)
            if not url:
                warnings.append(field+' 来源未提供')
                continue
            try:
                _, local_name = save(url,field+'.asset',image=field != 'preview_image',visual=field == 'preview_image')
                metadata[field+'_local'] = local_name
            except (Stopped, Cancelled):
                raise
            except Exception:
                warnings.append(field+' 下载失败')
        if metadata.get('vtt_url'):
            try:
                self.check_cancelled(task['id'])
                base = metadata['vtt_url']
                raw = save(base,'preview-source.vtt')[0].decode('utf-8-sig')
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
                        stem = 'preview' if number == 1 else f'preview-{number:03d}'
                        _, name = save(remote,stem+'.asset',image=True)
                        images[remote]=name
                    lines[i+1] = images[remote] + ('#'+reference.split('#',1)[1] if '#' in reference else '')
                self.file_downloader.save(folder/'preview.vtt','\n'.join(lines).encode())
                metadata['vtt_local'] = 'preview.vtt'
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
        else:
            warnings.append('VTT 来源未提供')
        return warnings
