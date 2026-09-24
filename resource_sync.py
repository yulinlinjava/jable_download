"""Manage third-party resource sites and synchronize resources to them."""
import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import requests
from Crypto.Cipher import AES


def _node_concurrency():
    try:
        return max(1, min(16, int(os.getenv('NODE_SYNC_CONCURRENCY', '1'))))
    except (TypeError, ValueError):
        return 1


class SyncError(Exception):
    pass


class ResourceSyncManager:
    NODE_CONCURRENCY = _node_concurrency()

    def __init__(self, database, exporter, media_resolver=None):
        self.database = Path(database)
        self.exporter = exporter
        self.media_resolver = media_resolver
        self._closed = threading.Event()
        self._wake = threading.Event()
        self._guard = threading.RLock()
        self._futures = {}
        self._executor = ThreadPoolExecutor(
            max_workers=self.NODE_CONCURRENCY, thread_name_prefix='node-sync')
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS resource_sync_sites (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                  endpoint_url TEXT NOT NULL, auth_type TEXT NOT NULL DEFAULT 'none',
                  header_name TEXT NOT NULL DEFAULT 'X-API-Key', secret TEXT,
                  resolve_media INTEGER NOT NULL DEFAULT 0,
                  batch_size INTEGER NOT NULL DEFAULT 50, timeout INTEGER NOT NULL DEFAULT 30,
                  created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS resource_sync_runs (
                  id TEXT PRIMARY KEY, status TEXT NOT NULL, total INTEGER NOT NULL,
                  succeeded INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
                  skipped INTEGER NOT NULL DEFAULT 0,
                  error TEXT, created REAL NOT NULL, completed REAL,
                  resolve_media INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS resource_sync_items (
                  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
                  source TEXT, video_id TEXT, title TEXT, status TEXT NOT NULL,
                  remote_id TEXT, message TEXT, UNIQUE(run_id,task_id));
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(resource_sync_runs)')}
            for name, definition in (
                ('site_id', 'TEXT'), ('site_name', 'TEXT'), ('scope', "TEXT NOT NULL DEFAULT 'selected'"),
                ('resolve_media', 'INTEGER NOT NULL DEFAULT 0'),
                ('skipped', 'INTEGER NOT NULL DEFAULT 0')):
                if name not in columns:
                    db.execute(f'ALTER TABLE resource_sync_runs ADD COLUMN {name} {definition}')
            site_columns = {row['name'] for row in db.execute(
                'PRAGMA table_info(resource_sync_sites)').fetchall()}
            if 'resolve_media' not in site_columns:
                db.execute('ALTER TABLE resource_sync_sites ADD COLUMN resolve_media INTEGER NOT NULL DEFAULT 0')
            item_columns = {row['name'] for row in db.execute(
                'PRAGMA table_info(resource_sync_items)').fetchall()}
            if 'id' not in item_columns:
                db.execute('ALTER TABLE resource_sync_items ADD COLUMN id TEXT')
            if 'title' not in item_columns:
                db.execute('ALTER TABLE resource_sync_items ADD COLUMN title TEXT')
            db.execute("UPDATE resource_sync_items SET id=lower(hex(randomblob(16))) WHERE id IS NULL OR id='' ")
            has_tasks = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()
            if has_tasks:
                db.execute('''UPDATE resource_sync_items SET title=(
                    SELECT title FROM tasks WHERE tasks.id=resource_sync_items.task_id)
                    WHERE title IS NULL OR title='' ''')
            db.execute('''UPDATE resource_sync_runs SET resolve_media=coalesce((
                SELECT resolve_media FROM resource_sync_sites
                WHERE resource_sync_sites.id=resource_sync_runs.site_id),resolve_media)''')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS resource_sync_items_id_unique ON resource_sync_items(id)')
            db.execute('CREATE INDEX IF NOT EXISTS resource_sync_items_status ON resource_sync_items(status)')
            db.execute('CREATE INDEX IF NOT EXISTS resource_sync_items_run ON resource_sync_items(run_id)')
            db.execute('''CREATE INDEX IF NOT EXISTS resource_sync_runs_site_kind
                          ON resource_sync_runs(site_id,resolve_media,created DESC)''')
            db.execute('''CREATE INDEX IF NOT EXISTS resource_sync_node_code_status
                          ON resource_sync_items(lower(trim(video_id)),status)''')
            db.execute("""UPDATE resource_sync_items SET status='queued',
                          message='服务重启，已恢复到等待发送队列'
                          WHERE status IN ('resolving','sending')""")
            db.execute("""UPDATE resource_sync_runs SET status='running',completed=NULL
                          WHERE resolve_media=1 AND EXISTS(
                            SELECT 1 FROM resource_sync_items i WHERE i.run_id=resource_sync_runs.id
                            AND i.status='queued')""")
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name='node-sync-dispatch', daemon=True)
        self._dispatcher.start()
        self._wake.set()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self):
        self._closed.set()
        self._wake.set()
        if self._dispatcher.is_alive():
            self._dispatcher.join(timeout=3)
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _key(self):
        path = self.database.with_suffix('.sync.key')
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + '.part')
            temporary.write_bytes(os.urandom(32))
            os.replace(temporary, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return hashlib.sha256(path.read_bytes()).digest()

    def _encrypt(self, value):
        cipher = AES.new(self._key(), AES.MODE_GCM)
        encrypted, tag = cipher.encrypt_and_digest(value.encode())
        return base64.urlsafe_b64encode(cipher.nonce + tag + encrypted).decode()

    def _decrypt(self, value):
        if not value:
            return ''
        try:
            raw = base64.urlsafe_b64decode(value)
            cipher = AES.new(self._key(), AES.MODE_GCM, nonce=raw[:16])
            return cipher.decrypt_and_verify(raw[32:], raw[16:32]).decode()
        except Exception as exc:
            raise SyncError('第三方网站密钥无法解密，请重新保存') from exc

    @staticmethod
    def serialize_site(row):
        return dict(id=row['id'], name=row['name'], enabled=bool(row['enabled']),
                    endpoint_url=row['endpoint_url'], auth_type=row['auth_type'],
                    header_name=row['header_name'], has_secret=bool(row['secret']),
                    resolve_media=bool(row['resolve_media']),
                    batch_size=row['batch_size'], timeout=row['timeout'],
                    created_at=row['created'], updated_at=row['updated'])

    def list_sites(self):
        with self.db() as db:
            rows = db.execute('SELECT * FROM resource_sync_sites ORDER BY created DESC').fetchall()
        return [self.serialize_site(row) for row in rows]

    def get_site(self, site_id, include_secret=False):
        with self.db() as db:
            row = db.execute('SELECT * FROM resource_sync_sites WHERE id=?', (site_id,)).fetchone()
        if not row:
            return None
        return row if include_secret else self.serialize_site(row)

    def validate_node_ids(self, node_ids):
        node_ids = list(dict.fromkeys(node_ids or []))
        invalid = []
        for site_id in node_ids:
            site = self.get_site(site_id, include_secret=True)
            if not site or not site['resolve_media'] or not site['enabled']:
                invalid.append(site_id)
        if invalid:
            raise SyncError('所选下载节点不存在、已停用或不是下载节点')
        return node_ids

    def attach_collection_nodes(self, tasks):
        node_ids = list(dict.fromkeys(
            site_id for task in tasks for site_id in (task.get('node_ids') or [])))
        sites = {}
        if node_ids:
            with self.db() as db:
                for offset in range(0, len(node_ids), 500):
                    chunk = node_ids[offset:offset + 500]
                    placeholders = ','.join('?' for _ in chunk)
                    sites.update({row['id']: row for row in db.execute(
                        f'''SELECT id,name,enabled,resolve_media FROM resource_sync_sites
                            WHERE id IN ({placeholders})''', chunk)})
        for task in tasks:
            task['sync_nodes'] = []
            for site_id in task.get('node_ids') or []:
                site = sites.get(site_id)
                available = bool(site and site['enabled'] and site['resolve_media'])
                task['sync_nodes'].append(dict(
                    id=site_id, name=site['name'] if site else '节点不可用',
                    enabled=bool(site['enabled']) if site else False,
                    available=available))
        return tasks

    def enqueue_node_items(self, node_ids, task_ids):
        """Reserve collection resources for every selected node without waiting for delivery."""
        task_ids = list(dict.fromkeys(task_ids or []))
        result = dict(queued=0, skipped=0, failed=0, failures=[])
        if not task_ids:
            return result
        for site_id in list(dict.fromkeys(node_ids or [])):
            site = self.get_site(site_id, include_secret=True)
            if not site or not site['resolve_media'] or not site['enabled']:
                result['failed'] += len(task_ids)
                result['failures'].append(dict(
                    node_id=site_id,
                    node_name=site['name'] if site else '节点不可用',
                    message='节点不存在、已停用或不是下载节点'))
                continue
            try:
                run_id, queued_ids, skipped = self._reserve_node_run(
                    site, task_ids, False, record_skipped=False)
                result['queued'] += len(queued_ids)
                result['skipped'] += skipped
                if queued_ids:
                    self._wake.set()
            except Exception as exc:
                result['failed'] += len(task_ids)
                result['failures'].append(dict(
                    node_id=site_id, node_name=site['name'], message=str(exc)[:300]))
        return result

    @staticmethod
    def _validate(name, endpoint_url, auth_type, header_name, batch_size, timeout):
        name = (name or '').strip()
        endpoint_url = (endpoint_url or '').strip()
        if not name:
            raise SyncError('请输入网站名称')
        if not endpoint_url:
            raise SyncError('请输入第三方接口地址')
        try:
            parts = urlsplit(endpoint_url)
            if (parts.scheme not in ('http', 'https') or not parts.hostname
                    or parts.username or parts.password):
                raise ValueError()
            parts.port
        except (TypeError, ValueError):
            raise SyncError('请输入有效的 HTTP/HTTPS 接口地址') from None
        if auth_type not in ('none', 'bearer', 'api_key'):
            raise SyncError('鉴权方式无效')
        if not isinstance(header_name, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,64}', header_name):
            raise SyncError('API Key 请求头名称无效')
        return (name, endpoint_url, auth_type, header_name,
                max(1, min(100, int(batch_size))), max(5, min(120, int(timeout))))

    @staticmethod
    def _node_import_endpoint(endpoint_url):
        """Allow a node base URL while still reusing its existing JSON import endpoint."""
        parts = urlsplit(endpoint_url)
        if parts.path in ('', '/') and not parts.query and not parts.fragment:
            return endpoint_url.rstrip('/') + '/api/collection-items/import'
        return endpoint_url

    def create_site(self, name, enabled, endpoint_url, auth_type, header_name,
                    secret, batch_size, timeout, resolve_media=False):
        name, endpoint_url, auth_type, header_name, batch_size, timeout = self._validate(
            name, endpoint_url, auth_type, header_name, batch_size, timeout)
        if resolve_media:
            endpoint_url = self._node_import_endpoint(endpoint_url)
        encrypted = self._encrypt(secret) if secret and auth_type != 'none' else None
        site_id, now = uuid.uuid4().hex, time.time()
        with self.db() as db:
            db.execute('''INSERT INTO resource_sync_sites
                (id,name,enabled,endpoint_url,auth_type,header_name,secret,resolve_media,
                 batch_size,timeout,created,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (site_id, name, 1 if enabled else 0, endpoint_url, auth_type, header_name,
                 encrypted, 1 if resolve_media else 0, batch_size, timeout, now, now))
        return self.get_site(site_id)

    def update_site(self, site_id, name, enabled, endpoint_url, auth_type, header_name,
                    secret, batch_size, timeout, resolve_media=False):
        name, endpoint_url, auth_type, header_name, batch_size, timeout = self._validate(
            name, endpoint_url, auth_type, header_name, batch_size, timeout)
        if resolve_media:
            endpoint_url = self._node_import_endpoint(endpoint_url)
        current = self.get_site(site_id, include_secret=True)
        if not current:
            raise SyncError('第三方网站不存在')
        encrypted = self._encrypt(secret) if secret else current['secret']
        if auth_type == 'none':
            encrypted = None
        with self.db() as db:
            db.execute('''UPDATE resource_sync_sites SET name=?,enabled=?,endpoint_url=?,auth_type=?,
                header_name=?,secret=?,resolve_media=?,batch_size=?,timeout=?,updated=? WHERE id=?''',
                (name, 1 if enabled else 0, endpoint_url, auth_type, header_name, encrypted,
                 1 if resolve_media else 0, batch_size, timeout, time.time(), site_id))
        return self.get_site(site_id)

    def delete_site(self, site_id):
        with self.db() as db:
            deleted = db.execute('DELETE FROM resource_sync_sites WHERE id=?', (site_id,)).rowcount
        return bool(deleted)

    @staticmethod
    def _read_response(response, limit=1024 * 1024):
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > limit:
                raise SyncError('第三方接口响应超过 1 MB')
            chunks.append(chunk)
        raw = b''.join(chunks)
        try:
            return json.loads(raw.decode('utf-8')) if raw else {}
        except (UnicodeDecodeError, ValueError):
            return {'message': raw.decode('utf-8', errors='replace')[:500]}

    def _send(self, endpoint, headers, payload, timeout):
        last_error = None
        for attempt in range(3):
            try:
                with requests.post(endpoint, json=payload, headers=headers, timeout=(10, timeout),
                                   stream=True, allow_redirects=False) as response:
                    data = self._read_response(response)
                    if 200 <= response.status_code < 300:
                        if isinstance(data, dict) and data.get('success') is False:
                            raise SyncError(str(data.get('message') or '第三方接口返回同步失败')[:500])
                        if isinstance(data, dict) and data.get('code') not in (None, 0):
                            raise SyncError(str(data.get('message') or '目标节点导入失败')[:500])
                        return data
                    message = str(data.get('message') or f'HTTP {response.status_code}')[:500]
                    if response.status_code < 500:
                        raise SyncError(message)
                    last_error = SyncError(message)
            except SyncError:
                raise
            except requests.RequestException as exc:
                last_error = SyncError('第三方接口连接失败：' + type(exc).__name__)
            if attempt < 2:
                time.sleep(attempt + 1)
        raise last_error or SyncError('第三方接口同步失败')

    def _headers(self, site):
        secret = self._decrypt(site['secret'])
        headers = {'Accept': 'application/json'}
        if site['auth_type'] == 'bearer':
            if not secret:
                raise SyncError('该网站尚未配置 Bearer Token')
            headers['Authorization'] = 'Bearer ' + secret
        elif site['auth_type'] == 'api_key':
            if not secret:
                raise SyncError('该网站尚未配置 API Key')
            headers[site['header_name']] = secret
        return headers

    def _all_task_ids(self, completed_only=False):
        with self.db() as db:
            query = "SELECT id FROM tasks WHERE status='completed' ORDER BY created" \
                if completed_only else 'SELECT id FROM tasks ORDER BY created'
            return [row['id'] for row in db.execute(query)]

    def _require_completed(self, task_ids):
        statuses = {}
        with self.db() as db:
            for offset in range(0, len(task_ids), 500):
                chunk = task_ids[offset:offset + 500]
                placeholders = ','.join('?' for _ in chunk)
                statuses.update({row['id']: row['status'] for row in db.execute(
                    f'SELECT id,status FROM tasks WHERE id IN ({placeholders})', chunk)})
        incomplete = [task_id for task_id in task_ids
                      if statuses.get(task_id) and statuses[task_id] != 'completed']
        if incomplete:
            raise SyncError(f'仅支持发送已下载完成的资源，当前选择中有 {len(incomplete)} 条未完成')

    def _task_descriptors(self, task_ids):
        by_id = {}
        with self.db() as db:
            for offset in range(0, len(task_ids), 500):
                chunk = task_ids[offset:offset + 500]
                placeholders = ','.join('?' for _ in chunk)
                for row in db.execute(
                        f'''SELECT id,source,video_id,title FROM tasks
                            WHERE id IN ({placeholders})''', chunk):
                    by_id[row['id']] = dict(row)
        missing = [task_id for task_id in task_ids if task_id not in by_id]
        if missing:
            raise SyncError('部分资源不存在，请刷新列表后重试')
        return [by_id[task_id] for task_id in task_ids]

    @staticmethod
    def _resource_code(row):
        return str(row.get('video_id') or row.get('id') or '').strip().lower()

    def _reserve_node_run(self, site, task_ids, all_resources, record_skipped=True):
        descriptors = self._task_descriptors(task_ids)
        run_id, now = uuid.uuid4().hex, time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing_codes = {str(record['video_id'] or '').strip().lower()
                              for record in db.execute('''SELECT i.video_id
                                FROM resource_sync_items i
                                JOIN resource_sync_runs r ON r.id=i.run_id
                                WHERE r.site_id=? AND r.resolve_media=1
                                AND i.status<>'failed' AND i.video_id IS NOT NULL''',
                                                       (site['id'],))}
            candidates, skipped = [], []
            for descriptor in descriptors:
                (skipped if self._resource_code(descriptor) in existing_codes
                 else candidates).append(descriptor)
            if not candidates and not record_skipped:
                return None, [], len(skipped)
            db.execute('''INSERT INTO resource_sync_runs
                (id,site_id,site_name,scope,status,total,succeeded,failed,skipped,
                 created,resolve_media)
                VALUES(?,?,?,?,?,?,?,?,?,?,1)''',
                (run_id, site['id'], site['name'], 'all' if all_resources else 'selected',
                 'running' if candidates else 'completed', len(candidates), 0, 0,
                 len(skipped), now))
            db.executemany('''INSERT INTO resource_sync_items
                (id,run_id,task_id,source,video_id,title,status,message)
                VALUES(?,?,?,?,?,?, 'queued','等待发送')''',
                [(uuid.uuid4().hex, run_id, descriptor['id'], descriptor['source'],
                  descriptor['video_id'], descriptor.get('title'))
                 for descriptor in candidates])
            if not candidates:
                db.execute('UPDATE resource_sync_runs SET completed=? WHERE id=?', (now, run_id))
        return run_id, [item['id'] for item in candidates], len(skipped)

    def sync(self, site_id, task_ids=None, all_resources=False):
        row = self.get_site(site_id, include_secret=True)
        if not row:
            raise SyncError('请选择有效的第三方网站')
        if not row['enabled']:
            raise SyncError('该第三方网站已停用')
        task_ids = (self._all_task_ids(completed_only=not row['resolve_media'])
                    if all_resources else list(dict.fromkeys(task_ids or [])))
        if not task_ids:
            raise SyncError('没有已下载完成的资源' if not row['resolve_media'] else '没有可同步的资源')
        if not row['resolve_media']:
            self._require_completed(task_ids)
        headers = self._headers(row)
        skipped = 0
        if row['resolve_media']:
            run_id, task_ids, skipped = self._reserve_node_run(row, task_ids, all_resources)
            self._wake.set()
            return self.get_run(run_id)
        else:
            run_id = uuid.uuid4().hex
        exported = self.exporter(task_ids)
        if exported['missing']:
            raise SyncError('部分资源不存在，请刷新列表后重试')
        resources = exported['items']
        if not resources:
            raise SyncError('没有可同步的资源')
        now = time.time()
        task_by_key = {(item['source'], item['id']): task_id
                       for task_id, item in zip(task_ids, resources)}
        if not row['resolve_media']:
            with self.db() as db:
                db.execute('''INSERT INTO resource_sync_runs
                    (id,site_id,site_name,scope,status,total,skipped,created,resolve_media)
                    VALUES(?,?,?,?,?,?,?,?,?)''',
                    (run_id, row['id'], row['name'],
                     'all' if all_resources else 'selected', 'running',
                     len(resources), skipped, now, row['resolve_media']))
        outcomes, send_resources = {}, []
        for item in resources:
            key = (item['source'], item['id'])
            if not row['resolve_media']:
                send_resources.append(item)
                continue
            if not self.media_resolver:
                outcomes[key] = ('failed', '', '当前服务没有配置播放地址解析器')
                continue
            try:
                resolved = self.media_resolver(dict(item))
                if not resolved.get('m3u8_url'):
                    raise SyncError('本地未解析到播放地址')
                send_resources.append(resolved)
            except Exception as exc:
                message = str(exc) if isinstance(exc, SyncError) else getattr(exc, 'message', None)
                outcomes[key] = ('failed', '', str(message or type(exc).__name__)[:500])
        for offset in range(0, len(send_resources), row['batch_size']):
            batch = send_resources[offset:offset + row['batch_size']]
            request_id = f'{run_id}-{offset // row["batch_size"] + 1}'
            try:
                payload = batch if row['resolve_media'] else {
                    'request_id': request_id, 'resources': batch}
                endpoint = (self._node_import_endpoint(row['endpoint_url'])
                            if row['resolve_media'] else row['endpoint_url'])
                response = self._send(endpoint, headers, payload, row['timeout'])
                if row['resolve_media']:
                    data = response.get('data') if isinstance(response, dict) else None
                    reported = data.get('results') if isinstance(data, dict) else None
                    reported_by_index = {
                        int(result.get('index')): result for result in (reported or [])
                        if isinstance(result, dict) and str(result.get('index', '')).isdigit()
                    }
                    for index, item in enumerate(batch, 1):
                        result = reported_by_index.get(index, {})
                        status = ('failed' if result.get('status') in ('failed', 'error')
                                  else 'succeeded')
                        outcomes[(item['source'], item['id'])] = (
                            status, str(result.get('task_id') or '')[:200],
                            str(result.get('message') or '')[:500])
                else:
                    reported = response.get('results') if isinstance(response, dict) else None
                    reported_by_key = {}
                    if isinstance(reported, list):
                        reported_by_key = {(str(item.get('source')), str(item.get('id'))): item
                                           for item in reported if isinstance(item, dict)}
                    for item in batch:
                        result = reported_by_key.get((item['source'], item['id']), {})
                        status = ('failed' if result.get('status') in ('failed', 'error')
                                  else 'succeeded')
                        outcomes[(item['source'], item['id'])] = (
                            status, str(result.get('remote_id') or '')[:200],
                            str(result.get('message') or '')[:500])
            except SyncError as exc:
                for item in batch:
                    outcomes[(item['source'], item['id'])] = ('failed', '', str(exc)[:500])
        with self.db() as db:
            for item in resources:
                status, remote_id, message = outcomes.get(
                    (item['source'], item['id']), ('failed', '', '资源未发送'))
                task_id = task_by_key[(item['source'], item['id'])]
                if row['resolve_media']:
                    db.execute('''UPDATE resource_sync_items SET source=?,video_id=?,title=?,
                        status=?,remote_id=?,message=? WHERE run_id=? AND task_id=?''',
                        (item['source'], item['id'], item.get('title'), status,
                         remote_id or None, message or None, run_id, task_id))
                else:
                    db.execute('''INSERT INTO resource_sync_items
                        (id,run_id,task_id,source,video_id,title,status,remote_id,message)
                        VALUES(?,?,?,?,?,?,?,?,?)''',
                        (uuid.uuid4().hex, run_id, task_id, item['source'], item['id'],
                         item.get('title'), status, remote_id or None, message or None))
            succeeded = sum(value[0] == 'succeeded' for value in outcomes.values())
            failed = len(resources) - succeeded
            first_failure = next((value[2] for value in outcomes.values()
                                  if value[0] == 'failed' and value[2]), '')
            error = (f'{failed} 条资源失败：{first_failure}'[:500]
                     if failed else None)
            db.execute('''UPDATE resource_sync_runs SET status=?,succeeded=?,failed=?,error=?,completed=?
                WHERE id=?''', ('failed' if failed else 'completed', succeeded, failed, error,
                                time.time(), run_id))
        return self.get_run(run_id)

    def _dispatch_loop(self):
        while not self._closed.is_set():
            self._wake.wait(timeout=1)
            self._wake.clear()
            try:
                self._dispatch_node_items()
            except Exception:
                pass

    def _dispatch_node_items(self):
        with self._guard:
            running = {item_id for item_id, future in self._futures.items()
                       if not future.done()}
            available = self.NODE_CONCURRENCY - len(running)
            if available <= 0:
                return
            with self.db() as db:
                rows = db.execute('''SELECT i.id FROM resource_sync_items i
                    JOIN resource_sync_runs r ON r.id=i.run_id
                    WHERE r.resolve_media=1 AND i.status='queued'
                    ORDER BY r.created,i.rowid LIMIT ?''', (available,)).fetchall()
                claimed = []
                for row in rows:
                    cursor = db.execute('''UPDATE resource_sync_items SET status='resolving',
                        message='正在获取资源数据' WHERE id=? AND status='queued' ''',
                                        (row['id'],))
                    if cursor.rowcount:
                        claimed.append(row['id'])
            for item_id in claimed:
                future = self._executor.submit(self._process_node_item, item_id)
                self._futures[item_id] = future
                future.add_done_callback(
                    lambda _future, current=item_id: self._node_item_done(current))

    def _node_item_done(self, item_id):
        with self._guard:
            self._futures.pop(item_id, None)
        self._wake.set()

    @staticmethod
    def _node_result(response):
        data = response.get('data') if isinstance(response, dict) else None
        results = data.get('results') if isinstance(data, dict) else None
        result = next((item for item in (results or []) if isinstance(item, dict)), {})
        if result.get('status') in ('failed', 'error'):
            raise SyncError(str(result.get('message') or '目标节点导入失败')[:500])
        return (str(result.get('task_id') or result.get('remote_id') or '')[:200],
                str(result.get('message') or '发送成功')[:500])

    def _refresh_node_run(self, db, run_id):
        counts = db.execute('''SELECT count(*) total,
            sum(CASE WHEN status IN ('succeeded','resolved') THEN 1 ELSE 0 END) succeeded,
            sum(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,
            sum(CASE WHEN status IN ('queued','resolving','sending') THEN 1 ELSE 0 END) active
            FROM resource_sync_items WHERE run_id=?''', (run_id,)).fetchone()
        succeeded, failed = int(counts['succeeded'] or 0), int(counts['failed'] or 0)
        active = int(counts['active'] or 0)
        failure = db.execute('''SELECT message FROM resource_sync_items
            WHERE run_id=? AND status='failed' AND message IS NOT NULL
            ORDER BY rowid LIMIT 1''', (run_id,)).fetchone()
        error = (f'{failed} 条资源失败：{failure["message"]}'[:500]
                 if failed and failure else None)
        status = 'running' if active else 'failed' if failed else 'completed'
        db.execute('''UPDATE resource_sync_runs SET status=?,succeeded=?,failed=?,error=?,
            completed=? WHERE id=?''',
                   (status, succeeded, failed, error, None if active else time.time(), run_id))

    def _process_node_item(self, item_id):
        run_id = None
        try:
            with self.db() as db:
                item = db.execute('''SELECT i.*,r.site_id FROM resource_sync_items i
                    JOIN resource_sync_runs r ON r.id=i.run_id WHERE i.id=?''',
                                  (item_id,)).fetchone()
            if not item:
                return
            run_id = item['run_id']
            site = self.get_site(item['site_id'], include_secret=True)
            if not site:
                raise SyncError('下载节点已删除')
            if not site['enabled']:
                raise SyncError('下载节点已停用')
            if not self.media_resolver:
                raise SyncError('当前服务没有配置播放地址解析器')
            exported = self.exporter([item['task_id']])
            if exported.get('missing') or not exported.get('items'):
                raise SyncError('资源不存在，请刷新列表后重试')
            resource = exported['items'][0]
            resolved = self.media_resolver(dict(resource))
            if not resolved.get('m3u8_url'):
                raise SyncError('本地未解析到播放地址')
            if self._closed.is_set():
                raise InterruptedError()
            with self.db() as db:
                db.execute('''UPDATE resource_sync_items SET source=?,video_id=?,title=?,
                    status='sending',message='资源数据获取成功，正在发送' WHERE id=?''',
                    (resolved.get('source'), resolved.get('id'), resolved.get('title'), item_id))
            response = self._send(self._node_import_endpoint(site['endpoint_url']),
                                  self._headers(site), [resolved], site['timeout'])
            remote_id, message = self._node_result(response)
            with self.db() as db:
                db.execute('''UPDATE resource_sync_items SET status='succeeded',remote_id=?,message=?
                              WHERE id=?''', (remote_id or None, message, item_id))
                db.execute('''UPDATE resource_sync_items SET status='resolved'
                    WHERE status='failed' AND id<>? AND lower(trim(video_id))=?
                    AND run_id IN (SELECT id FROM resource_sync_runs WHERE site_id=?)''',
                    (item_id, str(resolved.get('id') or '').strip().lower(), site['id']))
                self._refresh_node_run(db, run_id)
        except Exception as exc:
            if not run_id:
                return
            with self.db() as db:
                if self._closed.is_set() or isinstance(exc, InterruptedError):
                    db.execute("""UPDATE resource_sync_items SET status='queued',
                                  message='服务停止，等待下次启动继续发送' WHERE id=?""", (item_id,))
                else:
                    message = str(exc)[:500] or type(exc).__name__
                    db.execute("""UPDATE resource_sync_items SET status='failed',message=?
                                  WHERE id=?""", (message, item_id))
                self._refresh_node_run(db, run_id)

    def node_queue_summary(self):
        counts = dict(queued=0, processing=0, succeeded=0, failed=0)
        with self.db() as db:
            rows = db.execute('''WITH latest AS (
                    SELECT i.status,row_number() OVER (
                      PARTITION BY r.site_id,lower(trim(coalesce(i.video_id,i.task_id)))
                      ORDER BY r.created DESC,i.rowid DESC) position
                    FROM resource_sync_items i JOIN resource_sync_runs r ON r.id=i.run_id
                    WHERE r.resolve_media=1)
                SELECT status,count(*) total FROM latest WHERE position=1 GROUP BY status''').fetchall()
        for row in rows:
            status, total = row['status'], int(row['total'] or 0)
            if status == 'queued':
                counts['queued'] += total
            elif status in ('resolving', 'sending'):
                counts['processing'] += total
            elif status in ('succeeded', 'resolved'):
                counts['succeeded'] += total
            elif status == 'failed':
                counts['failed'] += total
        return counts

    def get_run(self, run_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM resource_sync_runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                return None
            items = [dict(item) for item in db.execute(
                'SELECT * FROM resource_sync_items WHERE run_id=? ORDER BY rowid', (run_id,))]
        first_failure = next((item.get('message') for item in items
                              if item.get('status') == 'failed' and item.get('message')), None)
        return dict(dict(row), items=items, failure_message=first_failure)

    def list_runs(self, limit=20, site_id=None):
        with self.db() as db:
            if site_id:
                rows = db.execute('''SELECT * FROM resource_sync_runs WHERE site_id=?
                    ORDER BY created DESC LIMIT ?''', (site_id, limit)).fetchall()
            else:
                rows = db.execute('SELECT * FROM resource_sync_runs ORDER BY created DESC LIMIT ?',
                                  (limit,)).fetchall()
        results = []
        with self.db() as db:
            for row in rows:
                value = dict(row)
                failure = db.execute('''SELECT message FROM resource_sync_items
                    WHERE run_id=? AND status='failed' AND message IS NOT NULL
                    ORDER BY rowid LIMIT 1''', (row['id'],)).fetchone()
                value['failure_message'] = failure['message'] if failure else None
                results.append(value)
        return results

    def retry(self, run_id):
        with self.db() as db:
            run = db.execute('SELECT site_id FROM resource_sync_runs WHERE id=?', (run_id,)).fetchone()
            rows = db.execute("SELECT task_id FROM resource_sync_items WHERE run_id=? AND status='failed'",
                              (run_id,)).fetchall()
        if not run:
            raise SyncError('同步记录不存在')
        if not run['site_id'] or not self.get_site(run['site_id']):
            raise SyncError('该记录对应的第三方网站已删除')
        if not rows:
            raise SyncError('该记录没有可重试的失败资源')
        return self.sync(run['site_id'], [row['task_id'] for row in rows])

    def list_records(self, page=1, page_size=20, keyword=None, status=None, site_id=None):
        if status and status not in ('queued', 'resolving', 'sending',
                                     'succeeded', 'failed', 'resolved'):
            raise SyncError('发送状态无效')
        where, values = ['r.resolve_media=1'], []
        if keyword:
            value = '%' + keyword.strip() + '%'
            where.append('''(coalesce(i.title,t.title,i.video_id) LIKE ? OR i.video_id LIKE ?
                             OR r.site_name LIKE ? OR i.source LIKE ?)''')
            values.extend((value, value, value, value))
        if status:
            where.append('i.status=?')
            values.append(status)
        if site_id:
            where.append('r.site_id=?')
            values.append(site_id)
        clause = ' WHERE ' + ' AND '.join(where)
        base = ''' FROM resource_sync_items i
                   JOIN resource_sync_runs r ON r.id=i.run_id
                   LEFT JOIN tasks t ON t.id=i.task_id'''
        with self.db() as db:
            total = db.execute('SELECT count(*)' + base + clause, values).fetchone()[0]
            rows = db.execute('''SELECT i.id,i.run_id,i.task_id,i.source,i.video_id,
                coalesce(i.title,t.title,i.video_id) title,i.status,i.remote_id,i.message,
                r.site_id,r.site_name,r.scope,r.created,r.completed''' + base + clause +
                ' ORDER BY r.created DESC,i.rowid DESC LIMIT ? OFFSET ?',
                (*values, page_size, (page - 1) * page_size)).fetchall()
        return dict(page=page, page_size=page_size, total=total,
                    items=[dict(row) for row in rows])

    def attach_node_statuses(self, items):
        if not items:
            return items
        task_ids = [str(item.get('id') or '') for item in items if item.get('id')]
        codes = list(dict.fromkeys(
            str(item.get('video_id') or '').strip().lower()
            for item in items if item.get('video_id')))
        conditions, values = [], []
        if task_ids:
            conditions.append('i.task_id IN (' + ','.join('?' for _ in task_ids) + ')')
            values.extend(task_ids)
        if codes:
            conditions.append('lower(trim(i.video_id)) IN (' + ','.join('?' for _ in codes) + ')')
            values.extend(codes)
        if not conditions:
            return items
        with self.db() as db:
            rows = db.execute('''SELECT i.task_id,i.video_id,i.status,i.message,
                r.site_id,coalesce(s.name,r.site_name) site_name,r.created,r.completed
                FROM resource_sync_items i
                JOIN resource_sync_runs r ON r.id=i.run_id
                LEFT JOIN resource_sync_sites s ON s.id=r.site_id
                WHERE r.resolve_media=1 AND (''' + ' OR '.join(conditions) + ''')
                ORDER BY r.created DESC,i.rowid DESC''', values).fetchall()
        by_task, by_code = {}, {}
        for raw in rows:
            row = dict(raw)
            site_id = row.get('site_id') or ''
            task_key = str(row.get('task_id') or '')
            code_key = str(row.get('video_id') or '').strip().lower()
            if task_key:
                by_task.setdefault(task_key, {}).setdefault(site_id, row)
            if code_key:
                by_code.setdefault(code_key, {}).setdefault(site_id, row)
        status_map = {'queued': 'queued', 'resolving': 'sending', 'sending': 'sending',
                      'failed': 'failed',
                      'succeeded': 'completed', 'resolved': 'completed'}
        for item in items:
            code = str(item.get('video_id') or '').strip().lower()
            records = by_code.get(code) or by_task.get(str(item.get('id') or '')) or {}
            item['synced_nodes'] = [
                dict(node_id=row['site_id'], node_name=row['site_name'] or '已删除节点',
                     status=status_map.get(row['status'], row['status']),
                     sent_at=row['completed'] or row['created'],
                     message=row['message'])
                for row in records.values()
            ]
        return items

    def _record_rows(self, record_ids=None, all_failed=False):
        with self.db() as db:
            if all_failed:
                return db.execute('''SELECT i.id,i.task_id,i.status,r.site_id,r.created
                    FROM resource_sync_items i JOIN resource_sync_runs r ON r.id=i.run_id
                    WHERE i.status='failed' AND r.resolve_media=1
                    ORDER BY r.created DESC,i.rowid DESC''').fetchall()
            record_ids = list(dict.fromkeys(record_ids or []))
            rows = []
            for offset in range(0, len(record_ids), 500):
                chunk = record_ids[offset:offset + 500]
                placeholders = ','.join('?' for _ in chunk)
                rows.extend(db.execute(f'''SELECT i.id,i.task_id,i.status,r.site_id,r.created
                    FROM resource_sync_items i JOIN resource_sync_runs r ON r.id=i.run_id
                    WHERE i.id IN ({placeholders}) AND r.resolve_media=1
                    ORDER BY r.created DESC,i.rowid DESC''', chunk).fetchall())
            return rows

    def retry_records(self, record_ids=None, all_failed=False):
        rows = self._record_rows(record_ids, all_failed)
        requested = len(rows)
        targets, seen = {}, set()
        skipped = 0
        for row in rows:
            key = (row['site_id'], row['task_id'])
            if row['status'] != 'failed' or key in seen:
                skipped += 1
                continue
            seen.add(key)
            targets.setdefault(row['site_id'], []).append(row['task_id'])
        if not targets:
            if all_failed:
                return dict(total=requested, retried=0, succeeded=0, failed=0,
                            queued=0, skipped=skipped, errors=[], runs=[])
            raise SyncError('没有可重试的失败发送记录')
        runs, queued, failed, errors = [], 0, 0, []
        for target_site_id, task_ids in targets.items():
            try:
                run = self.sync(target_site_id, task_ids)
                runs.append({key: value for key, value in run.items() if key != 'items'})
                queued += int(run.get('total') or 0)
                skipped += int(run.get('skipped') or 0)
            except SyncError as exc:
                failed += len(task_ids)
                errors.append(str(exc))
        return dict(total=requested, retried=len(seen), queued=queued, succeeded=0, failed=failed,
                    skipped=skipped, errors=list(dict.fromkeys(filter(None, errors))), runs=runs)

    def delete_records(self, record_ids):
        record_ids = list(dict.fromkeys(record_ids or []))
        if not record_ids:
            raise SyncError('请选择要删除的发送记录')
        run_ids, deleted, active = set(), 0, 0
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for offset in range(0, len(record_ids), 500):
                chunk = record_ids[offset:offset + 500]
                placeholders = ','.join('?' for _ in chunk)
                rows = db.execute(
                    f'''SELECT id,run_id,status FROM resource_sync_items
                        WHERE id IN ({placeholders})''', chunk).fetchall()
                deletable = [row for row in rows
                             if row['status'] not in ('queued', 'resolving', 'sending')]
                active += len(rows) - len(deletable)
                if deletable:
                    ids = [row['id'] for row in deletable]
                    marks = ','.join('?' for _ in ids)
                    run_ids.update(row['run_id'] for row in deletable)
                    deleted += db.execute(
                        f'DELETE FROM resource_sync_items WHERE id IN ({marks})', ids).rowcount
            for run_id in run_ids:
                counts = db.execute('''SELECT count(*) total,
                    sum(CASE WHEN status IN ('succeeded','resolved') THEN 1 ELSE 0 END) succeeded,
                    sum(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed
                    FROM resource_sync_items WHERE run_id=?''', (run_id,)).fetchone()
                if not counts['total']:
                    db.execute('DELETE FROM resource_sync_runs WHERE id=?', (run_id,))
                    continue
                failure = db.execute('''SELECT message FROM resource_sync_items
                    WHERE run_id=? AND status='failed' AND message IS NOT NULL
                    ORDER BY rowid LIMIT 1''', (run_id,)).fetchone()
                failed_count = int(counts['failed'] or 0)
                error = (f'{failed_count} 条资源失败：{failure["message"]}'[:500]
                         if failed_count and failure else None)
                db.execute('''UPDATE resource_sync_runs SET total=?,succeeded=?,failed=?,status=?,error=?
                    WHERE id=?''', (counts['total'], counts['succeeded'] or 0, failed_count,
                                    'failed' if failed_count else 'completed', error, run_id))
        return dict(requested=len(record_ids), deleted=deleted, active=active,
                    missing=len(record_ids) - deleted - active)
