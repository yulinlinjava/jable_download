"""Manage third-party resource sites and synchronize resources to them."""
import base64
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import requests
from Crypto.Cipher import AES

class SyncError(Exception):
    pass


class ResourceSyncManager:
    def __init__(self, database, exporter):
        self.database = Path(database)
        self.exporter = exporter
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS resource_sync_sites (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                  endpoint_url TEXT NOT NULL, auth_type TEXT NOT NULL DEFAULT 'none',
                  header_name TEXT NOT NULL DEFAULT 'X-API-Key', secret TEXT,
                  batch_size INTEGER NOT NULL DEFAULT 50, timeout INTEGER NOT NULL DEFAULT 30,
                  created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS resource_sync_runs (
                  id TEXT PRIMARY KEY, status TEXT NOT NULL, total INTEGER NOT NULL,
                  succeeded INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
                  error TEXT, created REAL NOT NULL, completed REAL);
                CREATE TABLE IF NOT EXISTS resource_sync_items (
                  run_id TEXT NOT NULL, task_id TEXT NOT NULL, source TEXT, video_id TEXT,
                  status TEXT NOT NULL, remote_id TEXT, message TEXT,
                  PRIMARY KEY(run_id,task_id));
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(resource_sync_runs)')}
            for name, definition in (
                ('site_id', 'TEXT'), ('site_name', 'TEXT'), ('scope', "TEXT NOT NULL DEFAULT 'selected'")):
                if name not in columns:
                    db.execute(f'ALTER TABLE resource_sync_runs ADD COLUMN {name} {definition}')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

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

    def create_site(self, name, enabled, endpoint_url, auth_type, header_name,
                    secret, batch_size, timeout):
        name, endpoint_url, auth_type, header_name, batch_size, timeout = self._validate(
            name, endpoint_url, auth_type, header_name, batch_size, timeout)
        encrypted = self._encrypt(secret) if secret and auth_type != 'none' else None
        site_id, now = uuid.uuid4().hex, time.time()
        with self.db() as db:
            db.execute('''INSERT INTO resource_sync_sites
                (id,name,enabled,endpoint_url,auth_type,header_name,secret,batch_size,timeout,created,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (site_id, name, 1 if enabled else 0, endpoint_url, auth_type, header_name,
                 encrypted, batch_size, timeout, now, now))
        return self.get_site(site_id)

    def update_site(self, site_id, name, enabled, endpoint_url, auth_type, header_name,
                    secret, batch_size, timeout):
        name, endpoint_url, auth_type, header_name, batch_size, timeout = self._validate(
            name, endpoint_url, auth_type, header_name, batch_size, timeout)
        current = self.get_site(site_id, include_secret=True)
        if not current:
            raise SyncError('第三方网站不存在')
        encrypted = self._encrypt(secret) if secret else current['secret']
        if auth_type == 'none':
            encrypted = None
        with self.db() as db:
            db.execute('''UPDATE resource_sync_sites SET name=?,enabled=?,endpoint_url=?,auth_type=?,
                header_name=?,secret=?,batch_size=?,timeout=?,updated=? WHERE id=?''',
                (name, 1 if enabled else 0, endpoint_url, auth_type, header_name, encrypted,
                 batch_size, timeout, time.time(), site_id))
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

    def _all_task_ids(self):
        with self.db() as db:
            return [row['id'] for row in db.execute('SELECT id FROM tasks ORDER BY created')]

    def sync(self, site_id, task_ids=None, all_resources=False):
        row = self.get_site(site_id, include_secret=True)
        if not row:
            raise SyncError('请选择有效的第三方网站')
        if not row['enabled']:
            raise SyncError('该第三方网站已停用')
        task_ids = self._all_task_ids() if all_resources else list(dict.fromkeys(task_ids or []))
        if not task_ids:
            raise SyncError('没有可同步的资源')
        exported = self.exporter(task_ids)
        if exported['missing']:
            raise SyncError('部分资源不存在，请刷新列表后重试')
        resources = exported['items']
        if not resources:
            raise SyncError('没有可同步的资源')
        secret = self._decrypt(row['secret'])
        headers = {'Accept': 'application/json'}
        if row['auth_type'] == 'bearer':
            if not secret:
                raise SyncError('该网站尚未配置 Bearer Token')
            headers['Authorization'] = 'Bearer ' + secret
        elif row['auth_type'] == 'api_key':
            if not secret:
                raise SyncError('该网站尚未配置 API Key')
            headers[row['header_name']] = secret
        run_id, now = uuid.uuid4().hex, time.time()
        task_by_key = {(item['source'], item['id']): task_id
                       for task_id, item in zip(task_ids, resources)}
        with self.db() as db:
            db.execute('''INSERT INTO resource_sync_runs
                (id,site_id,site_name,scope,status,total,created) VALUES(?,?,?,?,?,?,?)''',
                (run_id, row['id'], row['name'], 'all' if all_resources else 'selected',
                 'running', len(resources), now))
        outcomes = {}
        for offset in range(0, len(resources), row['batch_size']):
            batch = resources[offset:offset + row['batch_size']]
            request_id = f'{run_id}-{offset // row["batch_size"] + 1}'
            try:
                response = self._send(row['endpoint_url'], headers,
                                      {'request_id': request_id, 'resources': batch}, row['timeout'])
                reported = response.get('results') if isinstance(response, dict) else None
                reported_by_key = {}
                if isinstance(reported, list):
                    reported_by_key = {(str(item.get('source')), str(item.get('id'))): item
                                       for item in reported if isinstance(item, dict)}
                for item in batch:
                    result = reported_by_key.get((item['source'], item['id']), {})
                    status = 'failed' if result.get('status') in ('failed', 'error') else 'succeeded'
                    outcomes[(item['source'], item['id'])] = (
                        status, str(result.get('remote_id') or '')[:200],
                        str(result.get('message') or '')[:500])
            except SyncError as exc:
                for item in batch:
                    outcomes[(item['source'], item['id'])] = ('failed', '', str(exc)[:500])
        with self.db() as db:
            for item in resources:
                status, remote_id, message = outcomes[(item['source'], item['id'])]
                db.execute('''INSERT INTO resource_sync_items
                    (run_id,task_id,source,video_id,status,remote_id,message) VALUES(?,?,?,?,?,?,?)''',
                    (run_id, task_by_key[(item['source'], item['id'])], item['source'], item['id'],
                     status, remote_id or None, message or None))
            succeeded = sum(value[0] == 'succeeded' for value in outcomes.values())
            failed = len(resources) - succeeded
            db.execute('''UPDATE resource_sync_runs SET status=?,succeeded=?,failed=?,error=?,completed=?
                WHERE id=?''', ('failed' if failed else 'completed', succeeded, failed,
                                '部分或全部资源同步失败' if failed else None,
                                time.time(), run_id))
        return self.get_run(run_id)

    def get_run(self, run_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM resource_sync_runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                return None
            items = [dict(item) for item in db.execute(
                'SELECT * FROM resource_sync_items WHERE run_id=? ORDER BY rowid', (run_id,))]
        return dict(dict(row), items=items)

    def list_runs(self, limit=20, site_id=None):
        with self.db() as db:
            if site_id:
                rows = db.execute('''SELECT * FROM resource_sync_runs WHERE site_id=?
                    ORDER BY created DESC LIMIT ?''', (site_id, limit)).fetchall()
            else:
                rows = db.execute('SELECT * FROM resource_sync_runs ORDER BY created DESC LIMIT ?',
                                  (limit,)).fetchall()
        return [dict(row) for row in rows]

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
