"""Persistent Cloudflare R2 synchronization for completed user uploads."""
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from urllib.parse import quote, unquote, urlsplit

from Crypto.Cipher import AES
from download_tasks import SpeedMeter


class R2SyncError(Exception):
    pass


class R2SyncManager:
    MAX_CONCURRENCY = 16
    MAX_FILE_CONCURRENCY = 16
    AUTO_RETRY_DELAYS = (60, 300, 900, 1800)

    def __init__(self, download_root, database):
        self.database = Path(database).resolve()
        self.download_root = Path(download_root).resolve()
        self.upload_root = (self.download_root / 'uploads').resolve()
        self._guard = threading.RLock()
        self._closed = threading.Event()
        self._wake = threading.Event()
        self._backfill_wake = threading.Event()
        self._cleanup_wake = threading.Event()
        self._preview_guard = threading.RLock()
        self._preview_config = None
        self._preview_client = None
        self._preview_signature = None
        self._futures = {'video': {}, 'other': {}}
        self.speed_meter = SpeedMeter(window_seconds=5.0)
        self.group_speed_meters = {
            'video': SpeedMeter(window_seconds=5.0),
            'other': SpeedMeter(window_seconds=5.0),
        }
        self._executors = {
            'video': ThreadPoolExecutor(
                max_workers=self.MAX_CONCURRENCY, thread_name_prefix='r2-video'),
            'other': ThreadPoolExecutor(
                max_workers=self.MAX_CONCURRENCY, thread_name_prefix='r2-other'),
        }
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=NORMAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS r2_settings (
                  id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
                  endpoint_url TEXT, access_key_id TEXT, secret_access_key TEXT,
                  bucket TEXT, public_base_url TEXT, path_prefix TEXT,
                  concurrency INTEGER NOT NULL DEFAULT 1,
                  file_concurrency INTEGER NOT NULL DEFAULT 3,
                  video_concurrency INTEGER NOT NULL DEFAULT 1,
                  video_file_concurrency INTEGER NOT NULL DEFAULT 3,
                  other_concurrency INTEGER NOT NULL DEFAULT 3,
                  other_file_concurrency INTEGER NOT NULL DEFAULT 3,
                  auto_upload INTEGER NOT NULL DEFAULT 0,
                  auto_collection INTEGER NOT NULL DEFAULT 0,
                  cleanup_enabled INTEGER NOT NULL DEFAULT 0,
                  retention_days INTEGER NOT NULL DEFAULT 7,
                  created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS r2_sync_tasks (
                  id TEXT PRIMARY KEY, upload_identifier TEXT NOT NULL UNIQUE,
                  resource_type TEXT NOT NULL DEFAULT 'upload', resource_id TEXT,
                  source TEXT, title TEXT, sync_group TEXT NOT NULL DEFAULT 'other',
                  file_name TEXT, file_type TEXT, status TEXT NOT NULL DEFAULT 'queued',
                  total_files INTEGER NOT NULL DEFAULT 0,
                  synced_files INTEGER NOT NULL DEFAULT 0,
                  total_bytes INTEGER NOT NULL DEFAULT 0,
                  synced_bytes INTEGER NOT NULL DEFAULT 0,
                  object_key TEXT, public_url TEXT, message TEXT, error TEXT,
                  retry_count INTEGER NOT NULL DEFAULT 0, next_retry_at REAL,
                  created REAL NOT NULL, started REAL, completed REAL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS r2_sync_tasks_status_created
                  ON r2_sync_tasks(status,created,id);
                CREATE INDEX IF NOT EXISTS r2_sync_tasks_updated
                  ON r2_sync_tasks(updated DESC,id);
                CREATE TABLE IF NOT EXISTS r2_sync_files (
                  task_id TEXT NOT NULL, relative_path TEXT NOT NULL,
                  object_key TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL DEFAULT 'queued', error TEXT, updated REAL NOT NULL,
                  PRIMARY KEY(task_id,relative_path));
                CREATE INDEX IF NOT EXISTS r2_sync_files_task_status
                  ON r2_sync_files(task_id,status);
            ''')
            now = time.time()
            setting_columns = {row['name'] for row in db.execute(
                'PRAGMA table_info(r2_settings)').fetchall()}
            had_video_settings = 'video_concurrency' in setting_columns
            for name, definition in (
                    ('file_concurrency', 'INTEGER NOT NULL DEFAULT 3'),
                    ('video_concurrency', 'INTEGER NOT NULL DEFAULT 1'),
                    ('video_file_concurrency', 'INTEGER NOT NULL DEFAULT 3'),
                    ('other_concurrency', 'INTEGER NOT NULL DEFAULT 3'),
                    ('other_file_concurrency', 'INTEGER NOT NULL DEFAULT 3'),
                    ('auto_upload', 'INTEGER NOT NULL DEFAULT 0'),
                    ('auto_collection', 'INTEGER NOT NULL DEFAULT 0'),
                    ('cleanup_enabled', 'INTEGER NOT NULL DEFAULT 0'),
                    ('retention_days', 'INTEGER NOT NULL DEFAULT 7')):
                if name not in setting_columns:
                    db.execute(f'ALTER TABLE r2_settings ADD COLUMN {name} {definition}')
            task_columns = {row['name'] for row in db.execute(
                'PRAGMA table_info(r2_sync_tasks)').fetchall()}
            for name, definition in (
                    ('resource_type', "TEXT NOT NULL DEFAULT 'upload'"),
                    ('resource_id', 'TEXT'), ('source', 'TEXT'), ('title', 'TEXT'),
                    ('sync_group', "TEXT NOT NULL DEFAULT 'other'"),
                    ('retry_count', 'INTEGER NOT NULL DEFAULT 0'),
                    ('next_retry_at', 'REAL')):
                if name not in task_columns:
                    db.execute(f'ALTER TABLE r2_sync_tasks ADD COLUMN {name} {definition}')
            if not had_video_settings:
                db.execute('''UPDATE r2_settings SET
                              video_concurrency=coalesce(concurrency,1),
                              video_file_concurrency=coalesce(file_concurrency,3)''')
            db.execute("""UPDATE r2_sync_tasks SET resource_type='upload',
                          resource_id=upload_identifier
                          WHERE resource_id IS NULL OR resource_id=''""")
            db.execute("""UPDATE r2_sync_tasks SET sync_group=CASE
                          WHEN resource_type='collection' OR lower(file_type)='video'
                          THEN 'video' ELSE 'other' END""")
            db.execute('''CREATE INDEX IF NOT EXISTS r2_sync_tasks_group_status_created
                          ON r2_sync_tasks(sync_group,status,created,id)''')
            db.execute('''CREATE INDEX IF NOT EXISTS r2_sync_tasks_retry_schedule
                          ON r2_sync_tasks(sync_group,status,next_retry_at,created,id)''')
            db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS r2_sync_resource_unique
                          ON r2_sync_tasks(resource_type,resource_id)''')
            # Versions before generic resource synchronization stored paths
            # relative to downloads/uploads. Normalize them to DOWNLOAD_DIR.
            db.execute("""UPDATE r2_sync_files SET relative_path='uploads/'||relative_path
                          WHERE relative_path NOT LIKE 'uploads/%' AND EXISTS(
                            SELECT 1 FROM r2_sync_tasks t WHERE t.id=r2_sync_files.task_id
                            AND t.resource_type='upload')""")
            db.execute('''INSERT OR IGNORE INTO r2_settings
                (id,enabled,endpoint_url,access_key_id,secret_access_key,bucket,
                 public_base_url,path_prefix,concurrency,file_concurrency,
                 video_concurrency,video_file_concurrency,
                 other_concurrency,other_file_concurrency,
                 auto_upload,auto_collection,cleanup_enabled,retention_days,created,updated)
                VALUES(1,0,'','','','','','',1,3,1,3,3,3,0,0,0,7,?,?)''', (now, now))
            db.execute("""UPDATE r2_sync_tasks SET status='queued',started=NULL,
                          message='服务重启，任务已重新排队',updated=?
                          WHERE status='running'""", (now,))
            db.execute("""UPDATE r2_sync_files SET status='queued',error=NULL,updated=?
                          WHERE status='uploading'""", (now,))
            # Fill the direct resource flags once for databases that already
            # contain completed R2 logs from an earlier application version.
            tables = {row['name'] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if 'tasks' in tables:
                columns = {row['name'] for row in db.execute('PRAGMA table_info(tasks)').fetchall()}
                if {'r2_synced', 'r2_sync_status'} <= columns:
                    db.execute("""UPDATE tasks SET
                                  r2_synced=CASE WHEN EXISTS(
                                    SELECT 1 FROM r2_sync_tasks r
                                    WHERE r.resource_type='collection' AND r.resource_id=tasks.id
                                    AND r.status='completed') THEN 1 ELSE r2_synced END,
                                  r2_sync_status=CASE
                                    WHEN EXISTS(SELECT 1 FROM r2_sync_tasks r WHERE
                                      r.resource_type='collection' AND r.resource_id=tasks.id
                                      AND r.status='completed') THEN 'synced'
                                    WHEN EXISTS(SELECT 1 FROM r2_sync_tasks r WHERE
                                      r.resource_type='collection' AND r.resource_id=tasks.id
                                      AND r.status IN ('queued','running')) THEN 'syncing'
                                    ELSE r2_sync_status END""")
                    if 'r2_synced_at' in columns:
                        db.execute("""UPDATE tasks SET r2_synced_at=coalesce(r2_synced_at,(
                                      SELECT r.completed FROM r2_sync_tasks r
                                      WHERE r.resource_type='collection' AND r.resource_id=tasks.id
                                      AND r.status='completed' ORDER BY r.completed DESC LIMIT 1))
                                      WHERE r2_synced=1""")
            if 'uploads' in tables:
                columns = {row['name'] for row in db.execute('PRAGMA table_info(uploads)').fetchall()}
                if {'r2_synced', 'r2_sync_status'} <= columns:
                    db.execute("""UPDATE uploads SET
                                  r2_synced=CASE WHEN EXISTS(
                                    SELECT 1 FROM r2_sync_tasks r
                                    WHERE r.resource_type='upload' AND r.resource_id=uploads.identifier
                                    AND r.status='completed') THEN 1 ELSE r2_synced END,
                                  r2_sync_status=CASE
                                    WHEN EXISTS(SELECT 1 FROM r2_sync_tasks r WHERE
                                      r.resource_type='upload' AND r.resource_id=uploads.identifier
                                      AND r.status='completed') THEN 'synced'
                                    WHEN EXISTS(SELECT 1 FROM r2_sync_tasks r WHERE
                                      r.resource_type='upload' AND r.resource_id=uploads.identifier
                                      AND r.status IN ('queued','running')) THEN 'syncing'
                                    ELSE r2_sync_status END""")
                    if 'r2_synced_at' in columns:
                        db.execute("""UPDATE uploads SET r2_synced_at=coalesce(r2_synced_at,(
                                      SELECT r.completed FROM r2_sync_tasks r
                                      WHERE r.resource_type='upload' AND r.resource_id=uploads.identifier
                                      AND r.status='completed' ORDER BY r.completed DESC LIMIT 1))
                                      WHERE r2_synced=1""")
        self._set_preview_config(self.settings(include_secret=True))
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name='r2-sync-dispatch', daemon=True)
        self._backfill = threading.Thread(
            target=self._backfill_loop, name='r2-sync-backfill', daemon=True)
        self._cleanup = threading.Thread(
            target=self._cleanup_loop, name='r2-local-cleanup', daemon=True)
        self._dispatcher.start()
        self._backfill.start()
        self._cleanup.start()
        self._wake.set()
        self._backfill_wake.set()
        self._cleanup_wake.set()

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
        self._backfill_wake.set()
        self._cleanup_wake.set()
        if self._dispatcher.is_alive():
            self._dispatcher.join(timeout=3)
        if self._backfill.is_alive():
            self._backfill.join(timeout=3)
        if self._cleanup.is_alive():
            self._cleanup.join(timeout=3)
        for executor in self._executors.values():
            executor.shutdown(wait=False, cancel_futures=True)

    def upload_speed(self):
        return self.speed_meter.speed()

    def upload_speeds(self):
        return {
            'total': self.speed_meter.speed(),
            'video': self.group_speed_meters['video'].speed(),
            'other': self.group_speed_meters['other'].speed(),
        }

    def request_backfill(self):
        self._backfill_wake.set()
        self._cleanup_wake.set()

    def _key(self):
        path = self.database.with_suffix('.r2.key')
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
        if not value:
            return ''
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
            raise R2SyncError('R2 Secret Access Key 无法解密，请重新保存配置') from exc

    @staticmethod
    def _clean_url(value, required=False):
        value = str(value or '').strip().rstrip('/')
        if not value and not required:
            return ''
        try:
            parts = urlsplit(value)
            if (parts.scheme not in ('http', 'https') or not parts.hostname
                    or parts.username or parts.password or parts.query or parts.fragment):
                raise ValueError()
            parts.port
        except (TypeError, ValueError):
            raise R2SyncError('请输入有效的 HTTP/HTTPS 地址') from None
        return value

    @classmethod
    def _validate(cls, values, require_secret=True):
        endpoint = cls._clean_url(values.get('endpoint_url'), required=True)
        public_url = cls._clean_url(values.get('public_base_url'))
        access_key = str(values.get('access_key_id') or '').strip()
        secret = str(values.get('secret_access_key') or '')
        bucket = str(values.get('bucket') or '').strip()
        prefix = str(values.get('path_prefix') or '').strip().strip('/')
        if not access_key:
            raise R2SyncError('请输入 Access Key ID')
        if require_secret and not secret:
            raise R2SyncError('请输入 Secret Access Key')
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,255}', bucket):
            raise R2SyncError('Bucket 名称格式无效')
        if prefix and (len(prefix) > 512 or '\\' in prefix
                       or any(part in ('', '.', '..') for part in prefix.split('/'))):
            raise R2SyncError('对象路径前缀格式无效')
        if values.get('cleanup_enabled') and not public_url:
            raise R2SyncError('启用本地文件自动清理前，请配置 R2 公开地址')
        try:
            legacy_concurrency = values.get('concurrency')
            legacy_file_concurrency = values.get('file_concurrency')
            video_value = values.get('video_concurrency')
            video_file_value = values.get('video_file_concurrency')
            other_value = values.get('other_concurrency')
            other_file_value = values.get('other_file_concurrency')
            video_concurrency = int(video_value if video_value is not None else
                                    legacy_concurrency if legacy_concurrency is not None else 1)
            video_file_concurrency = int(
                video_file_value if video_file_value is not None else
                legacy_file_concurrency if legacy_file_concurrency is not None else 3)
            other_concurrency = int(other_value if other_value is not None else 3)
            other_file_concurrency = int(
                other_file_value if other_file_value is not None else 3)
            retention_days = int(values.get('retention_days', 7))
        except (TypeError, ValueError):
            raise R2SyncError('同步并发数格式无效') from None
        for label, concurrency in (
                ('视频同步任务并发数', video_concurrency),
                ('其他同步任务并发数', other_concurrency)):
            if concurrency < 0 or concurrency > cls.MAX_CONCURRENCY:
                raise R2SyncError(f'{label}必须为 0—{cls.MAX_CONCURRENCY}')
        for label, concurrency in (
                ('单视频资源文件并发数', video_file_concurrency),
                ('单普通资源文件并发数', other_file_concurrency)):
            if concurrency < 1 or concurrency > cls.MAX_FILE_CONCURRENCY:
                raise R2SyncError(f'{label}必须为 1—{cls.MAX_FILE_CONCURRENCY}')
        if retention_days > 3650:
            raise R2SyncError('本地文件保留天数不能超过 3650 天')
        retention_days = max(0, retention_days)
        return dict(enabled=bool(values.get('enabled')), endpoint_url=endpoint,
                    access_key_id=access_key, secret_access_key=secret,
                    bucket=bucket, public_base_url=public_url,
                    path_prefix=prefix,
                    video_concurrency=video_concurrency,
                    video_file_concurrency=video_file_concurrency,
                    other_concurrency=other_concurrency,
                    other_file_concurrency=other_file_concurrency,
                    auto_upload=bool(values.get('auto_upload')),
                    auto_collection=bool(values.get('auto_collection')),
                    cleanup_enabled=bool(values.get('cleanup_enabled')),
                    retention_days=retention_days)

    @staticmethod
    def _serialize_settings(row):
        return dict(enabled=bool(row['enabled']), endpointUrl=row['endpoint_url'] or '',
                    accessKeyId=row['access_key_id'] or '',
                    hasSecretAccessKey=bool(row['secret_access_key']),
                    bucket=row['bucket'] or '', publicBaseUrl=row['public_base_url'] or '',
                    pathPrefix=row['path_prefix'] or '',
                    videoConcurrency=int(row['video_concurrency'] or 0),
                    videoFileConcurrency=int(row['video_file_concurrency'] or 3),
                    otherConcurrency=int(row['other_concurrency'] or 0),
                    otherFileConcurrency=int(row['other_file_concurrency'] or 3),
                    # Keep the old fields readable for older management pages.
                    concurrency=int(row['video_concurrency'] or 0),
                    fileConcurrency=int(row['video_file_concurrency'] or 3),
                    autoUpload=bool(row['auto_upload']),
                    autoCollection=bool(row['auto_collection']),
                    cleanupEnabled=bool(row['cleanup_enabled']),
                    retentionDays=max(0, int(row['retention_days']
                                             if row['retention_days'] is not None else 7)),
                    maxConcurrency=R2SyncManager.MAX_CONCURRENCY,
                    maxFileConcurrency=R2SyncManager.MAX_FILE_CONCURRENCY,
                    updatedAt=row['updated'])

    def settings(self, include_secret=False):
        with self.db() as db:
            row = db.execute('SELECT * FROM r2_settings WHERE id=1').fetchone()
        result = dict(row)
        if include_secret:
            result['secret_access_key'] = self._decrypt(result.get('secret_access_key'))
            return result
        return self._serialize_settings(row)

    def save_settings(self, values):
        current = self.settings(include_secret=True)
        candidate = {
            'enabled': values.get('enabled', False),
            'endpoint_url': values.get('endpoint_url'),
            'access_key_id': values.get('access_key_id'),
            'secret_access_key': values.get('secret_access_key') or current.get('secret_access_key'),
            'bucket': values.get('bucket'), 'public_base_url': values.get('public_base_url'),
            'path_prefix': values.get('path_prefix'),
            'video_concurrency': (values.get('video_concurrency')
                                  if values.get('video_concurrency') is not None
                                  else values.get('concurrency')
                                  if values.get('concurrency') is not None
                                  else current.get('video_concurrency')),
            'video_file_concurrency': (values.get('video_file_concurrency')
                                       if values.get('video_file_concurrency') is not None
                                       else values.get('file_concurrency')
                                       if values.get('file_concurrency') is not None
                                       else current.get('video_file_concurrency')),
            'other_concurrency': (values.get('other_concurrency')
                                  if values.get('other_concurrency') is not None
                                  else current.get('other_concurrency', 3)),
            'other_file_concurrency': (values.get('other_file_concurrency')
                                       if values.get('other_file_concurrency') is not None
                                       else current.get('other_file_concurrency', 3)),
            'auto_upload': values.get('auto_upload', False),
            'auto_collection': values.get('auto_collection', False),
            'cleanup_enabled': values.get('cleanup_enabled', False),
            'retention_days': values.get('retention_days', 7),
        }
        validated = self._validate(candidate, require_secret=True)
        now = time.time()
        with self.db() as db:
            db.execute('''UPDATE r2_settings SET enabled=?,endpoint_url=?,access_key_id=?,
                          secret_access_key=?,bucket=?,public_base_url=?,path_prefix=?,
                          concurrency=?,file_concurrency=?,
                          video_concurrency=?,video_file_concurrency=?,
                          other_concurrency=?,other_file_concurrency=?,
                          auto_upload=?,auto_collection=?,
                          cleanup_enabled=?,retention_days=?,
                          updated=? WHERE id=1''',
                       (1 if validated['enabled'] else 0, validated['endpoint_url'],
                        validated['access_key_id'], self._encrypt(validated['secret_access_key']),
                        validated['bucket'], validated['public_base_url'],
                        validated['path_prefix'], validated['video_concurrency'],
                        validated['video_file_concurrency'],
                        validated['video_concurrency'], validated['video_file_concurrency'],
                        validated['other_concurrency'], validated['other_file_concurrency'],
                        1 if validated['auto_upload'] else 0,
                        1 if validated['auto_collection'] else 0,
                        1 if validated['cleanup_enabled'] else 0,
                        validated['retention_days'], now))
        self._set_preview_config(validated)
        self._wake.set()
        self._backfill_wake.set()
        self._cleanup_wake.set()
        return self.settings()

    @staticmethod
    def _client(config):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise R2SyncError('服务缺少 boto3，请重新构建镜像') from exc
        return boto3.client(
            's3', endpoint_url=config['endpoint_url'],
            aws_access_key_id=config['access_key_id'],
            aws_secret_access_key=config['secret_access_key'],
            region_name='auto', config=Config(
                signature_version='s3v4', connect_timeout=15, read_timeout=120,
                retries={'max_attempts': 3, 'mode': 'standard'}))

    def _set_preview_config(self, config):
        cached = dict(config or {})
        signature = tuple(cached.get(name) for name in (
            'endpoint_url', 'access_key_id', 'secret_access_key', 'bucket'))
        with self._preview_guard:
            if signature != self._preview_signature:
                self._preview_client = None
                self._preview_signature = signature
            self._preview_config = cached

    def open_preview_object(self, relative_path, range_header=None, head=False):
        """Open an upload asset from R2 without consulting resource/task tables."""
        relative = str(relative_path or '').replace('\\', '/').lstrip('/')
        if not relative or any(part in ('', '.', '..') for part in relative.split('/')):
            raise FileNotFoundError(relative)
        if range_header and not re.fullmatch(r'bytes=(?:\d+-\d*|-\d+)', range_header.strip()):
            raise ValueError('Range 请求格式无效')
        with self._preview_guard:
            config = dict(self._preview_config or {})
            if not all(config.get(name) for name in (
                    'endpoint_url', 'access_key_id', 'secret_access_key', 'bucket')):
                raise FileNotFoundError(relative)
            if self._preview_client is None:
                self._preview_client = self._client(config)
            client = self._preview_client
        key = self._object_key(config, relative)
        try:
            if head and not range_header:
                result = client.head_object(Bucket=config['bucket'], Key=key)
            else:
                arguments = {'Bucket': config['bucket'], 'Key': key}
                if range_header:
                    arguments['Range'] = range_header.strip()
                result = client.get_object(**arguments)
        except Exception as exc:
            try:
                from botocore.exceptions import ClientError
                response = exc.response if isinstance(exc, ClientError) else {}
                status = int(response.get('ResponseMetadata', {}).get('HTTPStatusCode') or 0)
                code = str(response.get('Error', {}).get('Code') or '')
            except (ImportError, TypeError, ValueError):
                status, code = 0, ''
            if status == 416 or code == 'InvalidRange':
                raise ValueError('请求范围超出文件大小') from exc
            if status in (403, 404) or code in ('NoSuchKey', 'NoSuchObject', 'AccessDenied'):
                raise FileNotFoundError(key) from exc
            raise
        headers = {'Accept-Ranges': 'bytes'}
        mappings = (
            ('ContentLength', 'Content-Length'), ('ContentRange', 'Content-Range'),
            ('ETag', 'ETag'), ('CacheControl', 'Cache-Control'),
            ('ContentDisposition', 'Content-Disposition'),
        )
        for source, target in mappings:
            if result.get(source) is not None:
                headers[target] = str(result[source])
        media_type = result.get('ContentType') or self._content_type(Path(relative))
        return {
            'body': result.get('Body'),
            'status': 206 if result.get('ContentRange') else 200,
            'headers': headers,
            'media_type': media_type,
            'object_key': key,
        }

    def test_settings(self, values):
        current = self.settings(include_secret=True)
        candidate = {
            'enabled': values.get('enabled', False),
            'endpoint_url': values.get('endpoint_url'),
            'access_key_id': values.get('access_key_id'),
            'secret_access_key': values.get('secret_access_key') or current.get('secret_access_key'),
            'bucket': values.get('bucket'), 'public_base_url': values.get('public_base_url'),
            'path_prefix': values.get('path_prefix'),
            'video_concurrency': (values.get('video_concurrency')
                                  if values.get('video_concurrency') is not None
                                  else values.get('concurrency')
                                  if values.get('concurrency') is not None
                                  else current.get('video_concurrency')),
            'video_file_concurrency': (values.get('video_file_concurrency')
                                       if values.get('video_file_concurrency') is not None
                                       else values.get('file_concurrency')
                                       if values.get('file_concurrency') is not None
                                       else current.get('video_file_concurrency')),
            'other_concurrency': (values.get('other_concurrency')
                                  if values.get('other_concurrency') is not None
                                  else current.get('other_concurrency', 3)),
            'other_file_concurrency': (values.get('other_file_concurrency')
                                       if values.get('other_file_concurrency') is not None
                                       else current.get('other_file_concurrency', 3)),
            'auto_upload': values.get('auto_upload', False),
            'auto_collection': values.get('auto_collection', False),
            'cleanup_enabled': values.get('cleanup_enabled', False),
            'retention_days': values.get('retention_days', 7),
        }
        config = self._validate(candidate, require_secret=True)
        try:
            client = self._client(config)
            client.head_bucket(Bucket=config['bucket'])
        except Exception as exc:
            raise R2SyncError('R2 连接测试失败：' + str(exc)[:300]) from exc
        return {'connected': True, 'bucket': config['bucket']}

    @staticmethod
    def _relative_from_url(value):
        path = unquote(urlsplit(str(value or '')).path).replace('\\', '/')
        marker = '/uploads/'
        if marker not in path:
            return None
        return path.split(marker, 1)[1].strip('/')

    def _upload_resource(self, metadata):
        relative = self._relative_from_url(metadata.get('hlsPath') or metadata.get('path'))
        if not relative:
            raise R2SyncError('上传记录缺少本地公开路径')
        target = (self.upload_root / relative).resolve()
        if self.upload_root not in target.parents:
            raise R2SyncError('上传记录的本地路径无效')
        if metadata.get('fileType') == 'video':
            directory = target.parent
            if not target.is_file() or not directory.is_dir():
                raise R2SyncError('视频成品目录不存在')
            files = sorted(path for path in directory.rglob('*')
                           if path.is_file() and not path.name.endswith('.part'))
        else:
            if not target.is_file():
                raise R2SyncError('上传文件不存在')
            files = [target]
        return ([(path, path.relative_to(self.download_root).as_posix()) for path in files],
                target.relative_to(self.download_root).as_posix())

    def _download_resource(self, row, metadata=None, folder=None):
        metadata = metadata or json.loads(row.get('metadata_json') or '{}')
        source = str(row.get('source') or '').strip().lower()
        source_directory = '123' if source == '123av' else \
            'jable' if source == 'jable' else 'external/' + source
        folder = Path(folder).resolve() if folder else (
            self.download_root / source_directory / str(row.get('video_id') or '')).resolve()
        if self.download_root not in folder.parents or not folder.is_dir():
            raise R2SyncError('采集资源本地目录不存在')
        files = sorted(path for path in folder.rglob('*')
                       if path.is_file() and not path.name.endswith(('.part', '.tmp')))
        if not files:
            raise R2SyncError('采集资源目录中没有可同步文件')
        local_playlist = str(metadata.get('m3u8_local') or 'index.m3u8')
        primary = (folder / local_playlist).resolve()
        if folder not in primary.parents and primary != folder:
            raise R2SyncError('采集资源播放清单路径无效')
        if not primary.is_file():
            primary = next((path for path in files if path.name == 'index.m3u8'), files[0])
        return ([(path, path.relative_to(self.download_root).as_posix()) for path in files],
                primary.relative_to(self.download_root).as_posix())

    @staticmethod
    def _object_key(config, relative):
        base = relative.lstrip('/')
        return f"{config['path_prefix']}/{base}" if config['path_prefix'] else base

    def _require_enabled(self):
        config = self.settings(include_secret=True)
        if not config.get('enabled'):
            raise R2SyncError('请先配置并启用R2')
        return config

    @staticmethod
    def _set_resource_status(db, resource_type, resource_id, status, synced_at=None):
        status = status if status in ('unsynced', 'syncing', 'synced') else 'unsynced'
        synced = status == 'synced'
        completed = (synced_at or time.time()) if synced else None
        if resource_type == 'upload':
            db.execute('''UPDATE uploads SET r2_synced=?,r2_sync_status=?,
                          r2_synced_at=CASE WHEN ?=1 THEN coalesce(r2_synced_at,?) ELSE NULL END,
                          updated=?
                          WHERE identifier=?''',
                       (1 if synced else 0, status, 1 if synced else 0,
                        completed, time.time(), resource_id))
        else:
            db.execute('''UPDATE tasks SET r2_synced=?,r2_sync_status=?,
                          r2_synced_at=CASE WHEN ?=1 THEN coalesce(r2_synced_at,?) ELSE NULL END,
                          updated=?
                          WHERE id=?''',
                       (1 if synced else 0, status, 1 if synced else 0,
                        completed, time.time(), resource_id))

    def _enqueue_resource(self, descriptor, files, primary_relative):
        config = self._require_enabled()
        resource_type = descriptor['resource_type']
        resource_id = str(descriptor['resource_id'])
        sync_group = ('video' if resource_type == 'collection'
                      or str(descriptor.get('file_type') or '').lower() == 'video'
                      else 'other')
        sync_key = f'{resource_type}:{resource_id}'
        now = time.time()
        total_bytes = sum(path.stat().st_size for path, _ in files)
        primary_key = self._object_key(config, primary_relative)
        public_url = (config['public_base_url'].rstrip('/') + '/' + quote(primary_key, safe='/')
                      if config.get('public_base_url') else None)
        with self.db() as db:
            existing = db.execute(
                '''SELECT id,status,completed,object_key FROM r2_sync_tasks
                   WHERE resource_type=? AND resource_id=?''',
                (resource_type, resource_id)).fetchone()
            same_destination = bool(existing and existing['object_key'] == primary_key)
            if existing and (existing['status'] == 'running' or
                             same_destination and existing['status'] in ('queued', 'completed')):
                self._set_resource_status(
                    db, resource_type, resource_id,
                    'synced' if existing['status'] == 'completed' else 'syncing',
                    existing['completed'])
                return {'task_id': existing['id'], 'action': 'reused',
                        'status': existing['status']}
            task_id = existing['id'] if existing else uuid.uuid4().hex
            db.execute('''INSERT INTO r2_sync_tasks(
                          id,upload_identifier,resource_type,resource_id,source,title,
                          sync_group,file_name,file_type,status,total_files,
                          synced_files,total_bytes,synced_bytes,object_key,public_url,
                          message,error,created,started,completed,updated)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(id) DO UPDATE SET file_name=excluded.file_name,
                          resource_type=excluded.resource_type,resource_id=excluded.resource_id,
                          source=excluded.source,title=excluded.title,
                          sync_group=excluded.sync_group,
                          file_type=excluded.file_type,status='queued',
                          total_files=excluded.total_files,synced_files=0,
                          total_bytes=excluded.total_bytes,synced_bytes=0,
                          object_key=excluded.object_key,public_url=excluded.public_url,
                          message='等待同步',error=NULL,started=NULL,completed=NULL,
                          retry_count=0,next_retry_at=NULL,
                          updated=excluded.updated''',
                       (task_id, sync_key, resource_type, resource_id,
                        descriptor.get('source'), descriptor.get('title'),
                        sync_group,
                        descriptor.get('file_name'), descriptor.get('file_type'),
                        'queued', len(files), 0, total_bytes, 0, primary_key, public_url,
                        '等待同步', None, now, None, None, now))
            db.execute('DELETE FROM r2_sync_files WHERE task_id=?', (task_id,))
            db.executemany('''INSERT INTO r2_sync_files
                              (task_id,relative_path,object_key,size,status,error,updated)
                              VALUES(?,?,?,?, 'queued',NULL,?)''',
                           [(task_id, relative, self._object_key(config, relative),
                             path.stat().st_size, now) for path, relative in files])
            self._set_resource_status(db, resource_type, resource_id, 'syncing')
        self._wake.set()
        return {'task_id': task_id, 'action': 'queued', 'status': 'queued'}

    def enqueue_upload(self, metadata, force=False):
        if not metadata or not metadata.get('completed'):
            return None
        if not force:
            config = self.settings(include_secret=True)
            if not (metadata.get('r2SyncEnabled') or config.get('auto_upload')):
                return None
        files, primary = self._upload_resource(metadata)
        identifier = str(metadata.get('identifier') or '')
        if not identifier:
            raise R2SyncError('上传记录缺少资源 ID')
        return self._enqueue_resource({
            'resource_type': 'upload', 'resource_id': identifier, 'source': 'upload',
            'title': metadata.get('fileName'), 'file_name': metadata.get('fileName'),
            'file_type': metadata.get('fileType'),
        }, files, primary)

    def enqueue_download(self, task, metadata, folder):
        if not task or task.get('status') == 'failed':
            return None
        config = self.settings(include_secret=True)
        collection_id = task.get('collection_id')
        collection_enabled = False
        if collection_id:
            with self.db() as db:
                collection = db.execute(
                    'SELECT r2_sync_enabled FROM collection_tasks WHERE id=?',
                    (collection_id,)).fetchone()
            collection_enabled = bool(collection and collection['r2_sync_enabled'])
        if not (config.get('auto_collection') or collection_enabled):
            return None
        files, primary = self._download_resource(task, metadata, folder)
        return self._enqueue_resource({
            'resource_type': 'collection', 'resource_id': task['id'],
            'source': task.get('source'), 'title': task.get('title') or task.get('video_id'),
            'file_name': task.get('video_id'), 'file_type': 'video',
        }, files, primary)

    def enqueue_upload_ids(self, identifiers):
        self._require_enabled()
        queued, reused, skipped = [], [], []
        with self.db() as db:
            placeholders = ','.join('?' for _ in identifiers)
            rows = db.execute(
                f'SELECT * FROM uploads WHERE identifier IN ({placeholders})', identifiers).fetchall()
        by_id = {row['identifier']: row for row in rows}
        for identifier in dict.fromkeys(identifiers):
            row = by_id.get(identifier)
            if not row or not bool(row['completed']):
                skipped.append({'id': identifier, 'reason': '上传资源尚未完成'})
                continue
            metadata = {
                'identifier': row['identifier'], 'fileName': row['file_name'],
                'fileType': row['file_type'], 'completed': True,
                'path': row['path'], 'hlsPath': row['hls_path'],
            }
            try:
                result = self.enqueue_upload(metadata, force=True)
                (queued if result['action'] == 'queued' else reused).append(identifier)
            except R2SyncError as exc:
                skipped.append({'id': identifier, 'reason': str(exc)})
        return {'queued': queued, 'reused': reused, 'skipped': skipped,
                'total': len(dict.fromkeys(identifiers))}

    def enqueue_download_ids(self, task_ids):
        self._require_enabled()
        queued, reused, skipped = [], [], []
        with self.db() as db:
            placeholders = ','.join('?' for _ in task_ids)
            rows = db.execute(
                f'SELECT * FROM tasks WHERE id IN ({placeholders})', task_ids).fetchall()
        by_id = {row['id']: dict(row) for row in rows}
        for task_id in dict.fromkeys(task_ids):
            row = by_id.get(task_id)
            if not row or row.get('status') != 'completed':
                skipped.append({'id': task_id, 'reason': '资源尚未下载完成'})
                continue
            try:
                metadata = json.loads(row.get('metadata_json') or '{}')
                files, primary = self._download_resource(row, metadata)
                result = self._enqueue_resource({
                    'resource_type': 'collection', 'resource_id': row['id'],
                    'source': row.get('source'),
                    'title': row.get('title') or row.get('video_id'),
                    'file_name': row.get('video_id'), 'file_type': 'video',
                }, files, primary)
                (queued if result['action'] == 'queued' else reused).append(task_id)
            except (R2SyncError, ValueError, TypeError) as exc:
                skipped.append({'id': task_id, 'reason': str(exc)})
        return {'queued': queued, 'reused': reused, 'skipped': skipped,
                'total': len(dict.fromkeys(task_ids))}

    def _backfill_loop(self):
        while not self._closed.is_set():
            self._backfill_wake.wait()
            self._backfill_wake.clear()
            if self._closed.is_set():
                return
            try:
                config = self.settings(include_secret=True)
                if not config.get('enabled'):
                    continue
                if config.get('auto_upload'):
                    self._backfill_type('upload')
                if config.get('auto_collection'):
                    self._backfill_type('collection')
            except Exception:
                # A fresh database can start this thread just before UploadManager
                # creates its table. The next settings change or periodic wake retries.
                pass

    def _cleanup_loop(self):
        while not self._closed.is_set():
            self._cleanup_wake.wait(timeout=300)
            self._cleanup_wake.clear()
            if self._closed.is_set():
                return
            try:
                self.cleanup_local_files()
            except Exception:
                # Cleanup is best effort. A later scan retries records whose
                # local_deleted flag was not committed.
                pass

    @staticmethod
    def _table_columns(db, table):
        return {row['name'] for row in db.execute(
            f'PRAGMA table_info({table})').fetchall()}

    def _remove_download_files(self, row):
        source = str(row.get('source') or '').strip().lower()
        source_directory = ('123' if source == '123av' else 'jable' if source == 'jable'
                            else 'external/' + source)
        folder = (self.download_root / source_directory / str(row.get('video_id') or '')).resolve()
        if folder == self.download_root or self.download_root not in folder.parents:
            raise R2SyncError('采集资源本地目录无效')
        if folder.exists():
            shutil.rmtree(folder)

    def _remove_upload_files(self, row):
        relative = self._relative_from_url(row.get('hls_path') or row.get('path'))
        if not relative:
            raise R2SyncError('上传记录缺少本地公开路径')
        target = (self.upload_root / relative).resolve()
        if target == self.upload_root or self.upload_root not in target.parents:
            raise R2SyncError('上传记录的本地路径无效')
        if row.get('file_type') == 'video':
            directory = target.parent
            if directory.exists():
                shutil.rmtree(directory)
            target = directory
        elif target.exists():
            target.unlink()
        parent = target.parent
        while parent != self.upload_root and self.upload_root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def _cleanup_local_resource(self, resource_type, resource_id):
        with self.db() as db:
            if resource_type == 'upload':
                row = db.execute(
                    """SELECT identifier,path,hls_path,file_type FROM uploads
                       WHERE identifier=? AND completed=1 AND r2_sync_status='synced'
                       AND r2_synced_at IS NOT NULL AND local_deleted=0""",
                    (resource_id,)).fetchone()
                key, table = 'identifier', 'uploads'
            else:
                row = db.execute(
                    """SELECT id,source,video_id FROM tasks
                       WHERE id=? AND status='completed' AND r2_sync_status='synced'
                       AND r2_synced_at IS NOT NULL AND local_deleted=0""",
                    (resource_id,)).fetchone()
                key, table = 'id', 'tasks'
        if not row:
            return False
        try:
            if resource_type == 'upload':
                self._remove_upload_files(dict(row))
            else:
                self._remove_download_files(dict(row))
            now = time.time()
            with self.db() as db:
                cursor = db.execute(
                    f"""UPDATE {table} SET local_deleted=1,local_deleted_at=?
                        WHERE {key}=? AND r2_sync_status='synced' AND local_deleted=0""",
                    (now, resource_id))
            return bool(cursor.rowcount)
        except (OSError, R2SyncError):
            return False

    def cleanup_local_files(self):
        config = self.settings()
        if not config.get('enabled') or not config.get('cleanupEnabled'):
            return {'deleted': 0}
        retention_days = config.get('retentionDays')
        retention_days = 7 if retention_days is None else max(0, int(retention_days))
        cutoff = time.time() - retention_days * 86400
        candidates = []
        with self.db() as db:
            tables = {row['name'] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if 'tasks' in tables and {'r2_synced_at', 'local_deleted'} <= self._table_columns(db, 'tasks'):
                candidates.extend(('collection', row['id']) for row in db.execute(
                    """SELECT id,source,video_id FROM tasks WHERE status='completed'
                       AND r2_sync_status='synced' AND r2_synced_at IS NOT NULL
                       AND r2_synced_at<=? AND local_deleted=0 ORDER BY r2_synced_at LIMIT 100""",
                    (cutoff,)).fetchall())
            if 'uploads' in tables and {'r2_synced_at', 'local_deleted'} <= self._table_columns(db, 'uploads'):
                candidates.extend(('upload', row['identifier']) for row in db.execute(
                    """SELECT identifier,path,hls_path,file_type FROM uploads WHERE completed=1
                       AND r2_sync_status='synced' AND r2_synced_at IS NOT NULL
                       AND r2_synced_at<=? AND local_deleted=0 ORDER BY r2_synced_at LIMIT 100""",
                    (cutoff,)).fetchall())
        deleted = 0
        for resource_type, resource_id in candidates:
            if self._closed.is_set():
                break
            deleted += int(self._cleanup_local_resource(resource_type, resource_id))
        if len(candidates) >= 100:
            self._cleanup_wake.set()
        return {'deleted': deleted}

    def cleanup_uploaded_local_files(self, resource_ids):
        """Delete local upload files after R2 synchronization, keeping all records."""
        deleted, skipped, failed = [], [], []
        for resource_id in dict.fromkeys(str(value).strip() for value in resource_ids):
            if not resource_id:
                skipped.append(resource_id)
                continue
            try:
                with self.db() as db:
                    row = db.execute(
                        """SELECT completed,r2_sync_status,local_deleted FROM uploads
                           WHERE identifier=?""", (resource_id,)).fetchone()
                if (not row or not row['completed'] or row['r2_sync_status'] != 'synced'
                        or row['local_deleted']):
                    skipped.append(resource_id)
                    continue
                if self._cleanup_local_resource('upload', resource_id):
                    deleted.append(resource_id)
                else:
                    failed.append(resource_id)
            except (OSError, R2SyncError, sqlite3.Error):
                failed.append(resource_id)
        return {'deleted': deleted, 'skipped': skipped, 'failed': failed}

    def _backfill_type(self, resource_type):
        cursor = ''
        while not self._closed.is_set():
            config = self.settings(include_secret=True)
            enabled_key = 'auto_upload' if resource_type == 'upload' else 'auto_collection'
            if not config.get('enabled') or not config.get(enabled_key):
                return
            with self.db() as db:
                if resource_type == 'upload':
                    rows = db.execute('''SELECT identifier id FROM uploads
                                         WHERE completed=1 AND identifier>?
                                         ORDER BY identifier LIMIT 200''',
                                      (cursor,)).fetchall()
                else:
                    rows = db.execute('''SELECT id FROM tasks
                                         WHERE status='completed' AND id>?
                                         ORDER BY id LIMIT 200''',
                                      (cursor,)).fetchall()
            if not rows:
                return
            ids = [row['id'] for row in rows]
            if resource_type == 'upload':
                self.enqueue_upload_ids(ids)
            else:
                self.enqueue_download_ids(ids)
            cursor = ids[-1]
            if len(rows) < 200:
                return

    def _dispatch_loop(self):
        while not self._closed.is_set():
            self._wake.wait(timeout=1)
            self._wake.clear()
            try:
                self._dispatch()
            except Exception:
                pass

    def _dispatch(self):
        config = self.settings(include_secret=True)
        if not config.get('enabled'):
            return
        with self._guard:
            for sync_group, setting_key in (
                    ('video', 'video_concurrency'), ('other', 'other_concurrency')):
                concurrency = max(0, min(
                    self.MAX_CONCURRENCY, int(config.get(setting_key) or 0)))
                if not concurrency:
                    continue
                futures = self._futures[sync_group]
                running = {task_id for task_id, future in futures.items()
                           if not future.done()}
                available = concurrency - len(running)
                if available <= 0:
                    continue
                with self.db() as db:
                    now = time.time()
                    rows = db.execute("""SELECT id FROM r2_sync_tasks
                                       WHERE status='queued' AND sync_group=?
                                       AND (next_retry_at IS NULL OR next_retry_at<=?)
                                       ORDER BY created,id LIMIT ?""",
                                      (sync_group, now, available)).fetchall()
                    claimed = []
                    for row in rows:
                        now = time.time()
                        cursor = db.execute("""UPDATE r2_sync_tasks SET status='running',
                                               message='正在同步',error=NULL,
                                               started=coalesce(started,?),next_retry_at=NULL,
                                               updated=? WHERE id=? AND status='queued'
                                               AND (next_retry_at IS NULL OR next_retry_at<=?)""",
                                            (now, now, row['id'], now))
                        if cursor.rowcount:
                            claimed.append(row['id'])
                for task_id in claimed:
                    future = self._executors[sync_group].submit(self._run, task_id)
                    futures[task_id] = future
                    future.add_done_callback(
                        lambda _future, group=sync_group, current=task_id:
                        self._done(group, current))

    def _done(self, sync_group, task_id):
        with self._guard:
            self._futures[sync_group].pop(task_id, None)
        self._wake.set()

    @staticmethod
    def _content_type(path):
        special = {'.m3u8': 'application/vnd.apple.mpegurl', '.ts': 'video/mp2t',
                   '.vtt': 'text/vtt'}
        return special.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] \
            or 'application/octet-stream'

    @staticmethod
    def _is_retryable_error(exc):
        """Only retry transient transport/service failures, not bad config or local data."""
        if isinstance(exc, R2SyncError):
            return False
        try:
            from botocore.exceptions import (
                ClientError, ConnectTimeoutError, ConnectionClosedError,
                EndpointConnectionError, HTTPClientError, ReadTimeoutError,
            )
            if isinstance(exc, (ConnectTimeoutError, ConnectionClosedError,
                                EndpointConnectionError, HTTPClientError,
                                ReadTimeoutError)):
                return True
            if isinstance(exc, ClientError):
                response = exc.response or {}
                try:
                    status = int(response.get(
                        'ResponseMetadata', {}).get('HTTPStatusCode') or 0)
                except (TypeError, ValueError):
                    status = 0
                code = str(response.get('Error', {}).get('Code') or '').lower()
                return (status in (408, 425, 429) or status >= 500
                        or code in {'requesttimeout', 'requesttimeoutexception', 'slowdown',
                                    'throttling', 'throttlingexception', 'internalerror',
                                    'serviceunavailable'})
        except ImportError:
            pass
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        # boto3 wraps some multipart failures in S3UploadFailedError and keeps
        # the transport cause only in the message.
        message = str(exc).lower()
        markers = (
            'timed out', 'timeout', 'connection reset', 'connection aborted',
            'connection closed', 'could not connect to the endpoint',
            'temporarily unavailable', 'service unavailable', 'slowdown',
            'too many requests', 'internalerror', 'bad gateway', 'gateway timeout',
        )
        return any(marker in message for marker in markers)

    @staticmethod
    def _retry_delay_text(seconds):
        return f'{seconds // 60} 分钟'

    def _update_progress(self, task_id, synced_files, synced_bytes, message):
        with self.db() as db:
            db.execute('''UPDATE r2_sync_tasks SET synced_files=?,synced_bytes=?,
                          message=?,updated=? WHERE id=?''',
                       (synced_files, synced_bytes, message, time.time(), task_id))

    def _run(self, task_id):
        try:
            config = self.settings(include_secret=True)
            if not config.get('enabled'):
                raise R2SyncError('R2 同步已停用')
            client = self._client(config)
            try:
                from boto3.s3.transfer import TransferConfig
            except ImportError as exc:
                raise R2SyncError('服务缺少 boto3，请重新构建镜像') from exc
            transfer = TransferConfig(multipart_threshold=16 * 1024 * 1024,
                                      multipart_chunksize=16 * 1024 * 1024,
                                      max_concurrency=2, use_threads=True)
            with self.db() as db:
                task = db.execute('SELECT * FROM r2_sync_tasks WHERE id=?', (task_id,)).fetchone()
                files = db.execute('SELECT * FROM r2_sync_files WHERE task_id=? ORDER BY relative_path',
                                   (task_id,)).fetchall()
            sync_group = task['sync_group'] if task['sync_group'] in ('video', 'other') else 'other'
            state = {
                'files': sum(1 for row in files if row['status'] == 'completed'),
                'bytes': sum(row['size'] for row in files if row['status'] == 'completed'),
                'inflight': {}, 'last_saved': 0.0,
            }
            state_lock = threading.Lock()

            def upload(row):
                if self._closed.is_set():
                    raise R2SyncError('服务正在关闭')
                if row['status'] == 'completed':
                    return
                local = (self.download_root / row['relative_path']).resolve()
                if self.download_root not in local.parents or not local.is_file():
                    raise R2SyncError('本地文件不存在：' + row['relative_path'])
                with self.db() as db:
                    db.execute("""UPDATE r2_sync_files SET status='uploading',error=NULL,
                                  updated=? WHERE task_id=? AND relative_path=?""",
                               (time.time(), task_id, row['relative_path']))
                def progress(amount):
                    self.speed_meter.record(int(amount))
                    self.group_speed_meters[sync_group].record(int(amount))
                    with state_lock:
                        state['inflight'][row['relative_path']] = (
                            state['inflight'].get(row['relative_path'], 0) + int(amount))
                        now = time.monotonic()
                        if now - state['last_saved'] < 1:
                            return
                        state['last_saved'] = now
                        synced_files = state['files']
                        synced_bytes = state['bytes'] + sum(state['inflight'].values())
                    self._update_progress(task_id, synced_files, synced_bytes,
                                          '正在并发同步资源文件')
                client.upload_file(
                    str(local), config['bucket'], row['object_key'],
                    ExtraArgs={'ContentType': self._content_type(local),
                               'CacheControl': 'public, max-age=31536000, immutable'},
                    Callback=progress, Config=transfer)
                with self.db() as db:
                    db.execute("""UPDATE r2_sync_files SET status='completed',error=NULL,
                                  updated=? WHERE task_id=? AND relative_path=?""",
                               (time.time(), task_id, row['relative_path']))
                with state_lock:
                    state['inflight'].pop(row['relative_path'], None)
                    state['files'] += 1
                    state['bytes'] += int(row['size'])
                    synced_files, synced_bytes = state['files'], state['bytes']
                self._update_progress(task_id, synced_files, synced_bytes,
                                      f'已同步 {synced_files} / {len(files)} 个文件')

            pending = [row for row in files if row['status'] != 'completed']
            # Publish playlists and VTT only after every referenced media/image
            # object is available, so a newly visible manifest is immediately usable.
            data_files = [row for row in pending
                          if Path(row['relative_path']).suffix.lower() not in ('.m3u8', '.vtt')]
            manifests = [row for row in pending
                         if Path(row['relative_path']).suffix.lower() in ('.m3u8', '.vtt')]
            file_setting = (config.get('video_file_concurrency') if sync_group == 'video'
                            else config.get('other_file_concurrency'))
            file_concurrency = max(1, min(
                self.MAX_FILE_CONCURRENCY, int(file_setting or 3)))
            if data_files:
                with ThreadPoolExecutor(max_workers=min(file_concurrency, len(data_files)),
                                        thread_name_prefix='r2-file') as executor:
                    futures = [executor.submit(upload, row) for row in data_files]
                    for future in as_completed(futures):
                        future.result()
            primary_path = task['object_key']
            manifests.sort(key=lambda row: (
                row['object_key'] == primary_path,
                Path(row['relative_path']).suffix.lower() == '.m3u8',
                row['relative_path']))
            for row in manifests:
                upload(row)
            now = time.time()
            with self.db() as db:
                db.execute("""UPDATE r2_sync_tasks SET status='completed',
                              synced_files=total_files,synced_bytes=total_bytes,
                              message='同步完成',error=NULL,next_retry_at=NULL,
                              completed=?,updated=? WHERE id=?""",
                           (now, now, task_id))
                self._set_resource_status(
                    db, task['resource_type'], task['resource_id'], 'synced', now)
            cleanup_config = self.settings()
            retention_days = cleanup_config.get('retentionDays')
            retention_days = 7 if retention_days is None else int(retention_days)
            if (cleanup_config.get('enabled') and cleanup_config.get('cleanupEnabled')
                    and retention_days <= 0):
                if not self._cleanup_local_resource(
                        task['resource_type'], task['resource_id']):
                    self._cleanup_wake.set()
        except Exception as exc:
            with self.db() as db:
                if self._closed.is_set():
                    now = time.time()
                    db.execute("""UPDATE r2_sync_tasks SET status='queued',
                                  message='服务停止，等待下次启动继续同步',error=NULL,
                                  started=NULL,completed=NULL,updated=? WHERE id=?""",
                               (now, task_id))
                    db.execute("""UPDATE r2_sync_files SET status='queued',error=NULL,
                                  updated=? WHERE task_id=? AND status='uploading'""",
                               (now, task_id))
                    resource = db.execute('''SELECT resource_type,resource_id
                                             FROM r2_sync_tasks WHERE id=?''',
                                          (task_id,)).fetchone()
                    if resource:
                        self._set_resource_status(
                            db, resource['resource_type'], resource['resource_id'], 'syncing')
                else:
                    message = str(exc)[:500] or type(exc).__name__
                    now = time.time()
                    resource = db.execute('''SELECT resource_type,resource_id,retry_count
                                             FROM r2_sync_tasks WHERE id=?''',
                                          (task_id,)).fetchone()
                    retry_count = int(resource['retry_count'] or 0) if resource else 0
                    retryable = (resource is not None and self._is_retryable_error(exc)
                                 and retry_count < len(self.AUTO_RETRY_DELAYS))
                    if retryable:
                        delay = self.AUTO_RETRY_DELAYS[retry_count]
                        retry_count += 1
                        next_retry = now + delay
                        db.execute("""UPDATE r2_sync_tasks SET status='queued',
                                      message=?,error=?,retry_count=?,next_retry_at=?,
                                      started=NULL,completed=NULL,updated=? WHERE id=?""",
                                   (f'网络异常，{self._retry_delay_text(delay)}后自动重试'
                                    f'（{retry_count}/{len(self.AUTO_RETRY_DELAYS)}）',
                                    message, retry_count, next_retry, now, task_id))
                        db.execute("""UPDATE r2_sync_files SET status='queued',error=NULL,
                                      updated=? WHERE task_id=? AND status='uploading'""",
                                   (now, task_id))
                        self._set_resource_status(
                            db, resource['resource_type'], resource['resource_id'], 'syncing')
                    else:
                        db.execute("""UPDATE r2_sync_tasks SET status='failed',
                                      message='同步失败',error=?,next_retry_at=NULL,
                                      completed=?,updated=? WHERE id=?""",
                                   (message, now, now, task_id))
                        if resource:
                            self._set_resource_status(
                                db, resource['resource_type'], resource['resource_id'], 'unsynced')

    @staticmethod
    def _serialize_task(row):
        total = int(row['total_bytes'] or 0)
        done = int(row['synced_bytes'] or 0)
        return dict(id=row['id'], resourceType=row['resource_type'],
                    resourceId=row['resource_id'], source=row['source'], title=row['title'],
                    syncGroup=row['sync_group'],
                    uploadIdentifier=(row['resource_id'] if row['resource_type'] == 'upload' else None),
                    fileName=row['file_name'], fileType=row['file_type'], status=row['status'],
                    totalFiles=int(row['total_files'] or 0),
                    syncedFiles=int(row['synced_files'] or 0),
                    totalBytes=total, syncedBytes=done,
                    progress=100.0 if row['status'] == 'completed' else
                    min(99.9, round(done / total * 100, 2)) if total else 0.0,
                    objectKey=row['object_key'], publicUrl=row['public_url'],
                    retryCount=int(row['retry_count'] or 0),
                    maxAutoRetries=len(R2SyncManager.AUTO_RETRY_DELAYS),
                    nextRetryAt=row['next_retry_at'],
                    message=row['message'], error=row['error'], createdAt=row['created'],
                    startedAt=row['started'], completedAt=row['completed'], updatedAt=row['updated'])

    def list_tasks(self, page=1, page_size=20, keyword=None, status=None,
                   resource_type=None, sync_group=None):
        conditions, values = [], []
        if keyword:
            escaped = keyword.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            conditions.append("(file_name LIKE ? ESCAPE '\\' OR resource_id LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' OR object_key LIKE ? ESCAPE '\\')")
            values.extend([f'%{escaped}%'] * 4)
        if status:
            conditions.append('status=?')
            values.append(status)
        if resource_type:
            conditions.append('resource_type=?')
            values.append(resource_type)
        if sync_group:
            conditions.append('sync_group=?')
            values.append(sync_group)
        clause = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
        with self.db() as db:
            total = db.execute('SELECT count(*) FROM r2_sync_tasks' + clause, values).fetchone()[0]
            rows = db.execute('SELECT * FROM r2_sync_tasks' + clause
                              + ' ORDER BY updated DESC,id DESC LIMIT ? OFFSET ?',
                              (*values, page_size, (page - 1) * page_size)).fetchall()
            group_counts = db.execute('''SELECT sync_group,status,count(*) total
                                         FROM r2_sync_tasks
                                         GROUP BY sync_group,status''').fetchall()
        grouped = {
            'video': {'queued': 0, 'running': 0, 'completed': 0, 'failed': 0},
            'other': {'queued': 0, 'running': 0, 'completed': 0, 'failed': 0},
        }
        counts = {}
        for row in group_counts:
            group = row['sync_group'] if row['sync_group'] in grouped else 'other'
            grouped[group][row['status']] = int(row['total'])
            counts[row['status']] = counts.get(row['status'], 0) + int(row['total'])
        return {'items': [self._serialize_task(row) for row in rows], 'total': total,
                'page': page, 'pageSize': page_size,
                'counts': counts,
                'groupCounts': grouped}

    def retry(self, task_ids):
        retried, skipped = [], []
        now = time.time()
        with self.db() as db:
            for task_id in dict.fromkeys(task_ids):
                row = db.execute('''SELECT status,resource_type,resource_id
                                    FROM r2_sync_tasks WHERE id=?''', (task_id,)).fetchone()
                if not row or row['status'] not in ('failed',):
                    skipped.append(task_id)
                    continue
                db.execute("""UPDATE r2_sync_tasks SET status='queued',message='等待重试',
                              error=NULL,retry_count=0,next_retry_at=NULL,
                              started=NULL,completed=NULL,updated=? WHERE id=?""",
                           (now, task_id))
                db.execute("""UPDATE r2_sync_files SET status='queued',error=NULL,updated=?
                              WHERE task_id=? AND status<>'completed'""", (now, task_id))
                self._set_resource_status(
                    db, row['resource_type'], row['resource_id'], 'syncing')
                retried.append(task_id)
        self._wake.set()
        return {'retried': retried, 'skipped': skipped}

    def delete_tasks(self, task_ids):
        deleted, skipped = [], []
        with self.db() as db:
            for task_id in dict.fromkeys(task_ids):
                row = db.execute('''SELECT status,resource_type,resource_id
                                    FROM r2_sync_tasks WHERE id=?''', (task_id,)).fetchone()
                if not row or row['status'] == 'running':
                    skipped.append(task_id)
                    continue
                if row['status'] != 'completed':
                    self._set_resource_status(
                        db, row['resource_type'], row['resource_id'], 'unsynced')
                db.execute('DELETE FROM r2_sync_files WHERE task_id=?', (task_id,))
                db.execute('DELETE FROM r2_sync_tasks WHERE id=?', (task_id,))
                deleted.append(task_id)
        return {'deleted': deleted, 'skipped': skipped}
