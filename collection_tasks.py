"""Persistent list collection and download scheduling on the unified tasks table."""
import json
import hashlib
import secrets
import logging
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo


log = logging.getLogger(__name__)


class CollectionError(Exception):
    pass


class CollectionManager:
    COLLECTOR_ONLINE_SECONDS = 60
    COLLECTOR_RETENTION_SECONDS = 7 * 24 * 60 * 60
    DOWNLOAD_PREPARE_TIMEOUT = 180
    # Resource lists return the full DownloadTask contract, but do not need
    # scheduler plans and other worker-only columns.  In particular, plans can
    # be large for videos with thousands of segments.
    ITEM_LIST_COLUMNS = '''id,collection_id,source,video_id,title,url,release_date,duration,
        cover_url,poster_file,preview_url,preview_file,vtt_url,vtt_file,m3u8_url,
        preview_m3u8_url,preview_m3u8_file,preview5_m3u8_url,preview5_m3u8_file,
        metadata_json,status,message,error,warnings,r2_synced,r2_sync_status,r2_synced_at,
        local_deleted,local_deleted_at,total_segments,downloaded_segments,downloaded_bytes,
        first_seen,last_seen,created,updated'''
    def __init__(self, root, downloads, collect, auto_start=True):
        self.database = downloads.database
        self.downloads = downloads
        self.collect = collect
        self.node_enqueue_callback = None
        self.site_enqueue_callback = None
        self.prepare_lock = threading.RLock()
        self._collector_cleanup_lock = threading.Lock()
        self._collector_cleanup_at = 0.0
        self.download_waiters = {}
        self.stop_event, self.wake, self.scheduler_wake = threading.Event(), threading.Event(), threading.Event()
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS collection_tasks (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
                  batch_size INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'idle',
                  scheduled_enabled INTEGER NOT NULL DEFAULT 0, last_scheduled_date TEXT,
                  r2_sync_enabled INTEGER NOT NULL DEFAULT 0,
                  local_collect_enabled INTEGER NOT NULL DEFAULT 0,
                  auto_download_enabled INTEGER NOT NULL DEFAULT 0,
                  node_ids TEXT NOT NULL DEFAULT '[]',
                  sync_site_ids TEXT NOT NULL DEFAULT '[]',
                  last_total INTEGER NOT NULL DEFAULT 0, last_created INTEGER NOT NULL DEFAULT 0,
                  last_skipped INTEGER NOT NULL DEFAULT 0, last_task_ids TEXT NOT NULL DEFAULT '[]',
                  last_node_queued INTEGER NOT NULL DEFAULT 0,
                  last_node_skipped INTEGER NOT NULL DEFAULT 0,
                  last_node_failed INTEGER NOT NULL DEFAULT 0,
                  last_download_queued INTEGER NOT NULL DEFAULT 0,
                  last_download_skipped INTEGER NOT NULL DEFAULT 0,
                  last_download_failed INTEGER NOT NULL DEFAULT 0,
                  last_error TEXT, last_run_at REAL, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS scheduler_settings (
                  id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 1,
                  concurrency INTEGER NOT NULL DEFAULT 1,
                  segment_concurrency INTEGER NOT NULL DEFAULT 3,
                  updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS collection_tasks_status_updated
                  ON collection_tasks(status,updated DESC);
                CREATE INDEX IF NOT EXISTS collection_tasks_scheduled_updated
                  ON collection_tasks(scheduled_enabled,updated DESC);
                CREATE TABLE IF NOT EXISTS collector_nodes (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL,
                  supported_sources TEXT NOT NULL DEFAULT '[]',
                  concurrency INTEGER NOT NULL DEFAULT 1,
                  last_heartbeat REAL NOT NULL, last_error TEXT,
                  created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS collector_jobs (
                  id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                  purpose TEXT NOT NULL DEFAULT 'download', source TEXT NOT NULL,
                  collection_id TEXT, resource_id TEXT, url TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'queued', assigned_node_id TEXT,
                  lease_token TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
                  error TEXT, created REAL NOT NULL, updated REAL NOT NULL, completed REAL);
                CREATE INDEX IF NOT EXISTS collector_jobs_status_created
                  ON collector_jobs(status,created);
                CREATE INDEX IF NOT EXISTS collector_jobs_collection
                  ON collector_jobs(collection_id,status);
                CREATE INDEX IF NOT EXISTS collector_jobs_resource
                  ON collector_jobs(resource_id,status);
                CREATE INDEX IF NOT EXISTS collector_jobs_node_status
                  ON collector_jobs(assigned_node_id,status,lease_until);
            ''')
            node_columns = {row['name'] for row in db.execute(
                'PRAGMA table_info(collector_nodes)').fetchall()}
            for name, definition in (
                    ('token_hash', 'TEXT'), ('token_suffix', 'TEXT'), ('token_value', 'TEXT'),
                    ('enabled', 'INTEGER NOT NULL DEFAULT 1'),
                    ('success_count', 'INTEGER NOT NULL DEFAULT 0'),
                    ('failed_count', 'INTEGER NOT NULL DEFAULT 0')):
                if name not in node_columns:
                    db.execute(f'ALTER TABLE collector_nodes ADD COLUMN {name} {definition}')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS collector_nodes_token ON collector_nodes(token_hash)')
            columns = {row['name'] for row in db.execute(
                'PRAGMA table_info(collection_tasks)').fetchall()}
            if 'r2_sync_enabled' not in columns:
                db.execute('''ALTER TABLE collection_tasks ADD COLUMN
                              r2_sync_enabled INTEGER NOT NULL DEFAULT 0''')
            if 'local_collect_enabled' not in columns:
                db.execute('''ALTER TABLE collection_tasks ADD COLUMN
                              local_collect_enabled INTEGER NOT NULL DEFAULT 0''')
            for name, definition in (
                    ('auto_download_enabled', 'INTEGER NOT NULL DEFAULT 0'),
                    ('node_ids', "TEXT NOT NULL DEFAULT '[]'"),
                    ('sync_site_ids', "TEXT NOT NULL DEFAULT '[]'"),
                    ('last_node_queued', 'INTEGER NOT NULL DEFAULT 0'),
                    ('last_node_skipped', 'INTEGER NOT NULL DEFAULT 0'),
                    ('last_node_failed', 'INTEGER NOT NULL DEFAULT 0'),
                    ('last_download_queued', 'INTEGER NOT NULL DEFAULT 0'),
                    ('last_download_skipped', 'INTEGER NOT NULL DEFAULT 0'),
                    ('last_download_failed', 'INTEGER NOT NULL DEFAULT 0')):
                if name not in columns:
                    db.execute(f'ALTER TABLE collection_tasks ADD COLUMN {name} {definition}')
            for name, definition in (
                    ('collection_type', "TEXT NOT NULL DEFAULT 'webpage'"),
                    ('api_source', "TEXT NOT NULL DEFAULT ''"),
                    ('request_method', "TEXT NOT NULL DEFAULT 'GET'"),
                    ('request_headers', "TEXT NOT NULL DEFAULT ''"),
                    ('request_body', "TEXT NOT NULL DEFAULT 'null'"),
                    ('request_timeout', 'INTEGER NOT NULL DEFAULT 30'),
                    ('last_updated', 'INTEGER NOT NULL DEFAULT 0'),
                    ('last_failed', 'INTEGER NOT NULL DEFAULT 0')):
                if name not in columns:
                    db.execute(f'ALTER TABLE collection_tasks ADD COLUMN {name} {definition}')
            task_columns = {row['name'] for row in db.execute(
                'PRAGMA table_info(tasks)').fetchall()}
            if 'sync_site_ids' not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN sync_site_ids TEXT NOT NULL DEFAULT '[]'")
            collector_job_columns = {row['name'] for row in db.execute(
                'PRAGMA table_info(collector_jobs)').fetchall()}
            if 'purpose' not in collector_job_columns:
                db.execute("""ALTER TABLE collector_jobs ADD COLUMN
                              purpose TEXT NOT NULL DEFAULT 'download'""")
            db.execute('''CREATE INDEX IF NOT EXISTS collector_jobs_dispatch_priority
                          ON collector_jobs(status,
                          CASE WHEN kind='detail' AND purpose='download' THEN 0 ELSE 1 END,created,id)''')
            db.execute("""UPDATE collector_jobs SET kind='detail',purpose='refresh'
                          WHERE kind='refresh_detail'""")
            scheduler_columns = {row['name'] for row in db.execute(
                'PRAGMA table_info(scheduler_settings)').fetchall()}
            if 'segment_concurrency' not in scheduler_columns:
                db.execute('''ALTER TABLE scheduler_settings ADD COLUMN
                              segment_concurrency INTEGER NOT NULL DEFAULT 3''')
            db.execute('''INSERT OR IGNORE INTO scheduler_settings
                          (id,enabled,concurrency,segment_concurrency,updated)
                          VALUES(1,1,1,3,?)''', (time.time(),))
            db.execute("""UPDATE collection_tasks SET status='idle',last_error='服务重启，采集已中断'
                          WHERE status IN ('queued','running')""")
            db.execute("""UPDATE collector_jobs SET status='queued',assigned_node_id=NULL,
                          lease_token=NULL,lease_until=NULL,updated=? WHERE status='leased'""",
                       (time.time(),))
            # Previously these jobs were created for every queued resource. They
            # must be renewed only after a download worker reserves a slot.
            db.execute("""UPDATE collector_jobs SET status='cancelled',assigned_node_id=NULL,
                          lease_token=NULL,lease_until=NULL,error='改为下载开始时重新解析',updated=?
                          WHERE kind='detail' AND purpose='download' AND status='queued'""", (time.time(),))
            db.execute("""UPDATE tasks SET status='queued',download_retry_at=0,
                          message='等待下载调度，开始下载时获取最新地址',updated=?
                          WHERE local_collect_enabled=1 AND status='waiting_collector'""", (time.time(),))
            self._initialize_resource_search(db)
        self.downloads.collector_available = self.has_online_collectors
        self.downloads.collector_resolver = self.resolve_download
        initial_settings = self.settings()
        self.downloads.set_scheduler(initial_settings['enabled'], initial_settings['concurrency'],
                                     initial_settings['segment_concurrency'])
        self.thread = threading.Thread(target=self.run_collection, name='collection-worker', daemon=True)
        self.scheduler_thread = threading.Thread(target=self.run_scheduler, name='resource-scheduler', daemon=True)
        if auto_start:
            self.start()

    def start(self):
        if not self.thread.is_alive():
            self.thread.start()
        if not self.scheduler_thread.is_alive():
            self.scheduler_thread.start()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def serialize(self, row):
        if not row:
            return None
        value = dict(row)
        if 'request_headers' in value:
            value['request_headers'] = (self.downloads.signers._decrypt(value['request_headers'])
                if value['request_headers'] else {})
        if 'request_body' in value:
            value['request_body'] = json.loads(value['request_body'] or 'null')
        if 'last_task_ids' in value:
            value['last_task_ids'] = json.loads(value['last_task_ids'] or '[]')
        if 'node_ids' in value:
            try:
                value['node_ids'] = json.loads(value['node_ids'] or '[]')
            except (TypeError, ValueError):
                value['node_ids'] = []
        if 'sync_site_ids' in value:
            try:
                value['sync_site_ids'] = json.loads(value['sync_site_ids'] or '[]')
            except (TypeError, ValueError):
                value['sync_site_ids'] = []
        if 'created' in value:
            value['created_at'] = value.pop('created')
        if 'updated' in value:
            value['updated_at'] = value.pop('updated')
        if 'scheduled_enabled' in value:
            value['scheduled_enabled'] = bool(value['scheduled_enabled'])
        if 'r2_sync_enabled' in value:
            value['r2_sync_enabled'] = bool(value['r2_sync_enabled'])
        if 'local_collect_enabled' in value:
            value['local_collect_enabled'] = bool(value['local_collect_enabled'])
        if 'auto_download_enabled' in value:
            value['auto_download_enabled'] = bool(value['auto_download_enabled'])
        return value

    @staticmethod
    def normalized_title(title):
        if not title:
            return None
        value = ' '.join(unicodedata.normalize('NFKC', title).split()).casefold()
        return value or None

    def _initialize_resource_search(self, db):
        """Create the title/video-id trigram index and keep it current cheaply."""
        self.resource_fts_enabled = False
        try:
            db.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
                          title,video_id,content='tasks',content_rowid='rowid',
                          tokenize='trigram')''')
            db.executescript('''
                CREATE TRIGGER IF NOT EXISTS tasks_fts_ai AFTER INSERT ON tasks BEGIN
                  INSERT INTO tasks_fts(rowid,title,video_id)
                  VALUES(new.rowid,coalesce(new.title,''),coalesce(new.video_id,''));
                END;
                CREATE TRIGGER IF NOT EXISTS tasks_fts_ad AFTER DELETE ON tasks BEGIN
                  INSERT INTO tasks_fts(tasks_fts,rowid,title,video_id)
                  VALUES('delete',old.rowid,coalesce(old.title,''),coalesce(old.video_id,''));
                END;
                CREATE TRIGGER IF NOT EXISTS tasks_fts_au
                AFTER UPDATE OF title,video_id ON tasks BEGIN
                  INSERT INTO tasks_fts(tasks_fts,rowid,title,video_id)
                  VALUES('delete',old.rowid,coalesce(old.title,''),coalesce(old.video_id,''));
                  INSERT INTO tasks_fts(rowid,title,video_id)
                  VALUES(new.rowid,coalesce(new.title,''),coalesce(new.video_id,''));
                END;
            ''')
            marker = db.execute(
                "SELECT value FROM app_settings WHERE key='tasks_fts_trigram_v1'").fetchone()
            if not marker:
                db.execute("INSERT INTO tasks_fts(tasks_fts) VALUES('rebuild')")
                db.execute('''INSERT OR REPLACE INTO app_settings(key,value,updated)
                              VALUES('tasks_fts_trigram_v1','1',?)''', (time.time(),))
            self.resource_fts_enabled = True
        except sqlite3.OperationalError as exc:
            log.warning('FTS5 trigram unavailable; resource search uses LIKE fallback: %s', exc)

    @staticmethod
    def _is_resource_path_search(value):
        value = str(value or '').strip()
        return '://' in value or value.startswith(('/', '\\'))

    @staticmethod
    def _resource_video_id(value):
        """Extract a video id from supported remote and local public paths."""
        supplied = str(value or '').strip().replace('\\', '/')
        try:
            path = unquote(urlsplit(supplied).path or supplied)
        except ValueError:
            path = supplied.split('?', 1)[0].split('#', 1)[0]
        parts = [part for part in path.split('/') if part]
        if not parts:
            return None
        lowered = [part.casefold() for part in parts]
        for marker in ('v', 'videos'):
            if marker in lowered:
                index = lowered.index(marker)
                if index + 1 < len(parts):
                    return parts[index + 1]
        for marker in ('jable', '123', '123av'):
            if marker in lowered:
                index = lowered.index(marker)
                if index + 1 < len(parts):
                    return parts[index + 1]
        if 'external' in lowered:
            index = lowered.index('external')
            if index + 2 < len(parts):
                return parts[index + 2]
        if parts[-1].casefold() in {
                'index.m3u8', 'preview.m3u8', 'preview5.m3u8', 'cover.jpg',
                'poster.jpg', 'preview.vtt'} and len(parts) >= 2:
            return parts[-2]
        return None

    def _resource_search_filter(self, value):
        value = ' '.join(unicodedata.normalize('NFKC', str(value or '')).split())
        if not value:
            return None, []
        if self._is_resource_path_search(value):
            video_id = self._resource_video_id(value)
            exact_url = value.split('#', 1)[0]
            if video_id:
                return ('(url=? OR lower(trim(video_id))=lower(trim(?)))',
                        [exact_url, video_id])
            return 'url=?', [exact_url]
        if len(value) < 3:
            raise CollectionError('标题或番号至少输入 3 个字符')
        if self.resource_fts_enabled:
            phrase = '"' + value.replace('"', '""') + '"'
            return ('''(lower(trim(video_id))=lower(trim(?)) OR rowid IN (
                       SELECT rowid FROM tasks_fts WHERE tasks_fts MATCH ?))''',
                    [value, phrase])
        escaped = value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        return ("(title LIKE ? ESCAPE '\\' OR video_id LIKE ? ESCAPE '\\')",
                [f'%{escaped}%', f'%{escaped}%'])

    def create(self, name, url, batch_size=1, scheduled_enabled=False,
               r2_sync_enabled=False, node_ids=None, local_collect_enabled=False,
               auto_download_enabled=False, sync_site_ids=None, **api_options):
        now, task_id = time.time(), uuid.uuid4().hex
        node_ids = list(dict.fromkeys(node_ids or []))
        sync_site_ids = list(dict.fromkeys(sync_site_ids or []))
        with self.db() as db:
            db.execute('''INSERT INTO collection_tasks(
                          id,name,url,batch_size,scheduled_enabled,r2_sync_enabled,
                          local_collect_enabled,auto_download_enabled,node_ids,sync_site_ids,
                          created,updated)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (task_id, name, url, 1, 1 if scheduled_enabled else 0,
                        1 if r2_sync_enabled else 0, 1 if local_collect_enabled else 0,
                        1 if auto_download_enabled else 0, json.dumps(node_ids),
                        json.dumps(sync_site_ids), now, now))
            self._save_api_options(db, task_id, api_options)
        return self.get(task_id)

    def create_many(self, tasks, scheduled_enabled=False, r2_sync_enabled=False,
                    node_ids=None, local_collect_enabled=False,
                    auto_download_enabled=False, sync_site_ids=None, **api_options):
        """Create collection tasks and skip duplicate names or webpage URLs."""
        created, skipped, results = [], [], []
        now = time.time()
        node_ids = list(dict.fromkeys(node_ids or []))
        sync_site_ids = list(dict.fromkeys(sync_site_ids or []))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing_rows = db.execute('''SELECT name,url,collection_type,api_source,
                request_method,request_headers,request_body FROM collection_tasks''').fetchall()
            names = {self.normalized_title(row['name']) for row in existing_rows}
            urls = {row['url'] for row in existing_rows if row['collection_type'] != 'api'}
            api_keys = set()
            for row in existing_rows:
                if row['collection_type'] != 'api':
                    continue
                try:
                    body = json.loads(row['request_body'] or 'null')
                except (TypeError, ValueError):
                    body = None
                try:
                    signers = getattr(self.downloads, 'signers', None)
                    if not row['request_headers']:
                        headers = {}
                    elif signers:
                        headers = signers._decrypt(row['request_headers'])
                    else:
                        headers = {'__stored__': row['request_headers']}
                except Exception:
                    headers = {'__stored__': row['request_headers']}
                api_keys.add(self.api_request_key(row['url'], dict(
                    api_source=row['api_source'], request_method=row['request_method'],
                    request_headers=headers, request_body=body)))
            for task in tasks:
                name, url = task['name'], task['url']
                name_key = self.normalized_title(name)
                options = dict(api_options)
                if options.get('collection_type') == 'api' and task.get('page') is not None:
                    options['request_body'] = self.page_values(
                        options.get('request_body'), task['page'])
                is_api = options.get('collection_type') == 'api'
                api_key = self.api_request_key(url, options) if is_api else None
                if name_key in names or (api_key in api_keys if is_api else url in urls):
                    skipped.append(dict(page=task.get('page'), name=name, url=url))
                    results.append(dict(page=task.get('page'), status='skipped',
                                        name=name, url=url,
                                        message='标题或接口请求已存在' if is_api else '标题或链接已存在'))
                    continue
                task_id = uuid.uuid4().hex
                db.execute('''INSERT INTO collection_tasks(
                    id,name,url,batch_size,scheduled_enabled,r2_sync_enabled,
                    local_collect_enabled,auto_download_enabled,node_ids,sync_site_ids,
                    created,updated)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (task_id, name, url, 1, 1 if scheduled_enabled else 0,
                            1 if r2_sync_enabled else 0, 1 if local_collect_enabled else 0,
                            1 if auto_download_enabled else 0,
                            json.dumps(node_ids), json.dumps(sync_site_ids), now, now))
                self._save_api_options(db, task_id, options)
                value = dict(id=task_id, page=task.get('page'), name=name, url=url)
                created.append(value)
                results.append(dict(**value, status='created', message='创建成功'))
                names.add(name_key)
                if is_api:
                    api_keys.add(api_key)
                else:
                    urls.add(url)
        return dict(total=len(tasks), created=len(created), skipped=len(skipped),
                    created_tasks=created, skipped_tasks=skipped, results=results)

    @staticmethod
    def page_values(value, page):
        if isinstance(value, str):
            return page if value == '${page}' else value.replace('${page}', str(page))
        if isinstance(value, list):
            return [CollectionManager.page_values(item, page) for item in value]
        if isinstance(value, dict):
            return {key: CollectionManager.page_values(item, page) for key, item in value.items()}
        return value

    @staticmethod
    def api_request_key(url, options):
        method = str(options.get('request_method') or 'GET').upper()
        body = options.get('request_body') if method == 'POST' else None
        headers = options.get('request_headers') or {}
        normalized_headers = {str(key).casefold(): value for key, value in headers.items()}
        return (
            url,
            str(options.get('api_source') or '').casefold(),
            method,
            json.dumps(normalized_headers, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':')),
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
        )

    def _save_api_options(self, db, task_id, options):
        from api_collection import validate_options, ApiCollectionError
        try:
            options = validate_options(**options)
        except ApiCollectionError as exc:
            raise CollectionError(str(exc)) from None
        headers = self.downloads.signers._encrypt(options['request_headers']) if options['request_headers'] else ''
        db.execute('''UPDATE collection_tasks SET collection_type=?,api_source=?,request_method=?,
            request_headers=?,request_body=?,request_timeout=? WHERE id=?''',
            (options['collection_type'], options['api_source'], options['request_method'], headers,
             json.dumps(options['request_body'], ensure_ascii=False), options['request_timeout'], task_id))

    def get(self, task_id):
        with self.db() as db:
            return self.serialize(db.execute('SELECT * FROM collection_tasks WHERE id=?', (task_id,)).fetchone())

    def list(self, page=1, page_size=20, keyword=None, status=None,
             scheduled_enabled=None, source=None):
        conditions, values = [], []
        if keyword:
            escaped = keyword.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            conditions.append("(name LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\')")
            values.extend((f'%{escaped}%', f'%{escaped}%'))
        if status:
            conditions.append('status=?')
            values.append(status)
        if scheduled_enabled is not None:
            conditions.append('scheduled_enabled=?')
            values.append(1 if scheduled_enabled else 0)
        if source == 'jable':
            conditions.append("((collection_type='webpage' AND lower(url) LIKE '%jable%') OR (collection_type='api' AND api_source='jable'))")
        elif source == '123av':
            conditions.append("((collection_type='webpage' AND lower(url) LIKE '%123av%') OR (collection_type='api' AND api_source='123av'))")
        elif source == 'other':
            conditions.append("((collection_type='webpage' AND lower(url) NOT LIKE '%jable%' AND lower(url) NOT LIKE '%123av%') OR (collection_type='api' AND api_source NOT IN ('jable','123av')))")
        clause = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
        with self.db() as db:
            total = db.execute('SELECT count(*) FROM collection_tasks' + clause, values).fetchone()[0]
            rows = db.execute(
                'SELECT * FROM collection_tasks' + clause
                + ' ORDER BY updated DESC,id DESC LIMIT ? OFFSET ?',
                (*values, page_size, (page - 1) * page_size)).fetchall()
            running = db.execute(
                "SELECT count(*) FROM collection_tasks WHERE status IN ('queued','running','waiting_collector')").fetchone()[0]
        return {
            'items': [self.serialize(row) for row in rows],
            'total': total,
            'page': page,
            'page_size': page_size,
            'running': running,
        }

    def update(self, task_id, name, url, batch_size=1, scheduled_enabled=False,
               r2_sync_enabled=False, node_ids=None, local_collect_enabled=False,
               auto_download_enabled=False, sync_site_ids=None, **api_options):
        node_ids = list(dict.fromkeys(node_ids or []))
        sync_site_ids = list(dict.fromkeys(sync_site_ids or []))
        with self.db() as db:
            row = db.execute('SELECT status FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                return None
            if row['status'] in ('queued', 'running', 'waiting_collector'):
                raise CollectionError('任务执行中，暂时不能修改')
            db.execute('''UPDATE collection_tasks SET name=?,url=?,batch_size=1,
                          scheduled_enabled=?,r2_sync_enabled=?,local_collect_enabled=?,
                          auto_download_enabled=?,node_ids=?,sync_site_ids=?,updated=? WHERE id=?''',
                       (name, url, 1 if scheduled_enabled else 0,
                        1 if r2_sync_enabled else 0, 1 if local_collect_enabled else 0,
                        1 if auto_download_enabled else 0,
                        json.dumps(node_ids), json.dumps(sync_site_ids), time.time(), task_id))
            self._save_api_options(db, task_id, api_options)
        return self.get(task_id)

    def delete(self, task_id):
        with self.db() as db:
            row = db.execute('SELECT status FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                return False
            if row['status'] in ('queued', 'running', 'waiting_collector'):
                raise CollectionError('任务执行中，暂时不能删除')
            db.execute('UPDATE tasks SET collection_id=NULL WHERE collection_id=?', (task_id,))
            db.execute('DELETE FROM collection_tasks WHERE id=?', (task_id,))
        return True

    def delete_many(self, task_ids):
        task_ids = list(dict.fromkeys(task_ids))
        placeholders = ','.join('?' for _ in task_ids)
        deleted, skipped, missing = [], [], []
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute(
                f'SELECT id,status FROM collection_tasks WHERE id IN ({placeholders})',
                task_ids).fetchall()
            rows_by_id = {row['id']: row for row in rows}
            for task_id in task_ids:
                row = rows_by_id.get(task_id)
                if not row:
                    missing.append(task_id)
                elif row['status'] in ('queued', 'running', 'waiting_collector'):
                    skipped.append(task_id)
                else:
                    deleted.append(task_id)
            if deleted:
                deleted_placeholders = ','.join('?' for _ in deleted)
                db.execute(
                    f'UPDATE tasks SET collection_id=NULL WHERE collection_id IN ({deleted_placeholders})',
                    deleted)
                db.execute(
                    f'DELETE FROM collection_tasks WHERE id IN ({deleted_placeholders})', deleted)
        return {
            'total': len(task_ids), 'deleted': len(deleted),
            'skipped': len(skipped), 'missing': len(missing),
            'deleted_ids': deleted, 'skipped_ids': skipped, 'missing_ids': missing,
        }

    def execute(self, task_id):
        with self.db() as db:
            row = db.execute('SELECT status FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                return None
            if row['status'] in ('queued', 'running', 'waiting_collector'):
                raise CollectionError('任务已在执行')
            db.execute("UPDATE collection_tasks SET status='queued',last_error=NULL,updated=? WHERE id=?",
                       (time.time(), task_id))
        self.wake.set()
        return self.get(task_id)

    def execute_many(self, task_ids):
        queued, skipped, missing = [], [], []
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for task_id in dict.fromkeys(task_ids):
                row = db.execute('SELECT status FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
                if not row:
                    missing.append(task_id)
                elif row['status'] in ('queued', 'running', 'waiting_collector'):
                    skipped.append(task_id)
                else:
                    db.execute("UPDATE collection_tasks SET status='queued',last_error=NULL,updated=? WHERE id=?",
                               (time.time(), task_id))
                    queued.append(task_id)
        if queued:
            self.wake.set()
        return dict(queued=len(queued), skipped=len(skipped), missing=len(missing), queued_ids=queued)

    @staticmethod
    def collector_source(url):
        host = (urlsplit(url).hostname or '').casefold()
        return '123av' if '123av' in host else 'jable' if 'jable' in host else 'other'

    @staticmethod
    def _collector_job(row):
        if not row:
            return None
        value = dict(row)
        for field in ('created', 'updated', 'completed'):
            if field in value:
                value[field + '_at'] = value.pop(field)
        return value

    def _enqueue_collector_job(self, db, kind, url, collection_id=None, resource_id=None,
                               purpose='download'):
        field, value = ('collection_id', collection_id) if collection_id else ('resource_id', resource_id)
        existing = db.execute(
            f"""SELECT id,status FROM collector_jobs WHERE {field}=?
                AND kind=? AND purpose=? ORDER BY created DESC LIMIT 1""",
            (value, kind, purpose)).fetchone()
        if existing:
            if existing['status'] not in ('queued', 'leased'):
                now = time.time()
                db.execute("""UPDATE collector_jobs SET source=?,url=?,status='queued',
                              assigned_node_id=NULL,lease_token=NULL,lease_until=NULL,
                              error=NULL,completed=NULL,updated=? WHERE id=?""",
                           (self.collector_source(url), url, now, existing['id']))
            return existing['id']
        now, job_id = time.time(), uuid.uuid4().hex
        db.execute('''INSERT INTO collector_jobs(
                      id,kind,purpose,source,collection_id,resource_id,url,status,created,updated)
                      VALUES(?,?,?,?,?,?,?,'queued',?,?)''',
                   (job_id, kind, purpose, self.collector_source(url), collection_id,
                    resource_id, url, now, now))
        return job_id

    def has_online_collectors(self):
        if self.stop_event.is_set():
            return False
        with self.db() as db:
            return bool(db.execute('''SELECT 1 FROM collector_nodes WHERE enabled=1
                AND token_hash IS NOT NULL AND last_heartbeat>? LIMIT 1''',
                (time.time() - self.COLLECTOR_ONLINE_SECONDS,)).fetchone())

    def resolve_download(self, task):
        """The caller keeps its download slot until a fresh detail result arrives."""
        from download_tasks import Cancelled, DownloadError, PreparationDeferred, Stopped
        if not self.has_online_collectors():
            raise PreparationDeferred('等待代理采集节点上线，暂时释放下载名额')
        waiter = dict(event=threading.Event(), error=None, result=None)
        with self.prepare_lock:
            if task['id'] in self.download_waiters:
                raise DownloadError('资源已有正在执行的地址解析任务')
            try:
                with self.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    current = db.execute('SELECT * FROM tasks WHERE id=?', (task['id'],)).fetchone()
                    if not current:
                        raise Cancelled()
                    waiter['job_id'] = self._enqueue_collector_job(
                        db, 'detail', current['url'], resource_id=task['id'])
                    self.download_waiters[task['id']] = waiter
                    db.execute("""UPDATE tasks SET status='waiting_collector',error=NULL,
                                  message='已预留下载名额，等待代理采集最新播放地址',updated=? WHERE id=?""",
                               (time.time(), task['id']))
            except Exception:
                self.download_waiters.pop(task['id'], None)
                raise
        deadline = time.monotonic() + self.DOWNLOAD_PREPARE_TIMEOUT
        try:
            while not waiter['event'].wait(min(1, max(0, deadline - time.monotonic()))):
                self.downloads.check_cancelled(task['id'])
                if self.stop_event.is_set() or self.downloads.stop_event.is_set():
                    raise Stopped()
                if time.monotonic() >= deadline:
                    raise PreparationDeferred('代理采集最新地址超时，30秒后重新调度')
            if waiter['error']:
                raise DownloadError(waiter['error'])
            self.downloads.check_cancelled(task['id'])
            if self.stop_event.is_set() or self.downloads.stop_event.is_set():
                raise Stopped()
            return waiter['result']
        finally:
            with self.prepare_lock:
                self.download_waiters.pop(task['id'], None)
                with self.db() as db:
                    db.execute("""UPDATE collector_jobs SET status='cancelled',assigned_node_id=NULL,
                                  lease_token=NULL,lease_until=NULL,updated=?
                                  WHERE id=? AND status IN ('queued','leased')""",
                               (time.time(), waiter['job_id']))

    def authenticate_collector(self, token):
        if not re.fullmatch(r'[A-Za-z0-9_-]{22}', str(token or '')):
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db() as db:
            row = db.execute('SELECT id,name,enabled,token_value FROM collector_nodes WHERE token_hash=?',
                             (digest,)).fetchone()
            if row and not row['token_value']:
                db.execute('UPDATE collector_nodes SET token_value=? WHERE id=? AND token_value IS NULL',
                           (token, row['id']))
        return {key: row[key] for key in ('id', 'name', 'enabled')} if row else None

    def create_collector_node(self, name, enabled=True):
        name = str(name or '').strip()
        if not name:
            raise CollectionError('请输入节点名称')
        token, node_id, now = secrets.token_urlsafe(16), uuid.uuid4().hex, time.time()
        with self.db() as db:
            db.execute('''INSERT INTO collector_nodes(id,name,token_hash,token_suffix,token_value,enabled,
                          last_heartbeat,created,updated) VALUES(?,?,?,?,?,?,0,?,?)''',
                       (node_id, name[:100], hashlib.sha256(token.encode()).hexdigest(),
                        token[-4:], token, int(enabled), now, now))
        return dict(id=node_id, token=token)

    def update_collector_node(self, node_id, name, enabled):
        name = str(name or '').strip()
        if not name:
            raise CollectionError('请输入节点名称')
        with self.db() as db:
            changed = db.execute('''UPDATE collector_nodes SET name=?,enabled=?,updated=?
                                    WHERE id=? AND token_hash IS NOT NULL''',
                                 (name[:100], int(enabled), time.time(), node_id)).rowcount
        return bool(changed)

    def rotate_collector_token(self, node_id):
        token, now = secrets.token_urlsafe(16), time.time()
        with self.db() as db:
            changed = db.execute('''UPDATE collector_nodes SET token_hash=?,token_suffix=?,token_value=?,
                                    last_heartbeat=0,updated=?
                                    WHERE id=? AND token_hash IS NOT NULL''',
                                 (hashlib.sha256(token.encode()).hexdigest(), token[-4:], token, now,
                                  node_id)).rowcount
            if changed:
                self._release_collector_jobs(db, node_id, now)
        return dict(id=node_id, token=token) if changed else None

    @staticmethod
    def _release_collector_jobs(db, node_id, now):
        db.execute('''UPDATE collector_jobs SET status='queued',assigned_node_id=NULL,
                      lease_token=NULL,lease_until=NULL,updated=?
                      WHERE assigned_node_id=? AND status='leased' ''', (now, node_id))

    def delete_collector_node(self, node_id):
        with self.db() as db:
            changed = db.execute('DELETE FROM collector_nodes WHERE id=? AND token_hash IS NOT NULL',
                                 (node_id,)).rowcount
            if changed:
                self._release_collector_jobs(db, node_id, time.time())
        return bool(changed)

    def claim_collector_job(self, node_id):
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            node = db.execute('SELECT enabled FROM collector_nodes WHERE id=? AND token_hash IS NOT NULL',
                              (node_id,)).fetchone()
            if not node:
                raise CollectionError('采集节点不存在')
            db.execute('UPDATE collector_nodes SET last_heartbeat=?,updated=? WHERE id=?',
                       (now, now, node_id))
            if not node['enabled']:
                return None
            expired = db.execute("""SELECT id,collection_id,resource_id FROM collector_jobs
                WHERE status='leased' AND lease_until<?""", (now,)).fetchall()
            for job in expired:
                db.execute("""UPDATE collector_jobs SET status='queued',assigned_node_id=NULL,
                              lease_token=NULL,lease_until=NULL,error='采集节点租约超时',updated=?
                              WHERE id=?""", (now, job['id']))
            row = db.execute(
                """SELECT * FROM collector_jobs WHERE status='queued'
                    ORDER BY CASE WHEN kind='detail' AND purpose='download' THEN 0 ELSE 1 END,
                             created,id LIMIT 1""").fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            db.execute("""UPDATE collector_jobs SET status='leased',assigned_node_id=?,
                          lease_token=?,lease_until=?,attempts=attempts+1,error=NULL,updated=?
                          WHERE id=?""", (node_id, token, now + 120, now, row['id']))
            value = dict(row)
            value.update(status='leased', assigned_node_id=node_id, lease_token=token,
                         lease_until=now + 120, attempts=int(row['attempts']) + 1, updated=now)
        value = self._collector_job(value)
        if value['kind'] == 'list' and value.get('collection_id'):
            task = self.get(value['collection_id'])
            if task and task['collection_type'] == 'api':
                value.update(collection_type='api', api_request={key: task[key] for key in (
                    'request_method','request_headers','request_body','request_timeout')})
                value['source'] = task['api_source'] or 'other'
        return value

    def heartbeat_collector(self, node_id, job_id=None, lease_token=None):
        now = time.time()
        with self.db() as db:
            node = db.execute('SELECT id FROM collector_nodes WHERE id=?', (node_id,)).fetchone()
            if not node:
                return False
            db.execute('UPDATE collector_nodes SET last_heartbeat=?,updated=? WHERE id=?',
                       (now, now, node_id))
            if job_id:
                updated = db.execute("""UPDATE collector_jobs SET lease_until=?,updated=?
                    WHERE id=? AND status='leased' AND assigned_node_id=? AND lease_token=?
                    AND lease_until>?""",
                    (now + 120, now, job_id, node_id, lease_token, now)).rowcount
                return bool(updated)
        return True

    def list_collector_nodes(self, include_token=False):
        now = time.time()
        cleanup = False
        if now >= self._collector_cleanup_at:
            with self._collector_cleanup_lock:
                if now >= self._collector_cleanup_at:
                    cleanup = True
                    self._collector_cleanup_at = now + 300
        with self.db() as db:
            if cleanup:
                db.execute('DELETE FROM collector_nodes WHERE token_hash IS NULL AND last_heartbeat<?',
                           (now - self.COLLECTOR_RETENTION_SECONDS,))
            rows = db.execute('''SELECT n.*, (SELECT count(*) FROM collector_jobs j
                                WHERE j.assigned_node_id=n.id AND j.status='leased'
                                AND j.lease_until>?) current_jobs
                                FROM collector_nodes n WHERE n.token_hash IS NOT NULL
                                ORDER BY n.created DESC''', (now,)).fetchall()
            counts = {row['status']: row['count'] for row in db.execute(
                'SELECT status,count(*) count FROM collector_jobs GROUP BY status').fetchall()}
        items = []
        for row in rows:
            value = dict(row)
            value.pop('token_hash', None)
            stored_token = value.pop('token_value', None)
            if include_token:
                value['token'] = stored_token
            value.pop('supported_sources', None)
            value.pop('concurrency', None)
            value['enabled'] = bool(value['enabled'])
            value['online'] = now - value['last_heartbeat'] <= self.COLLECTOR_ONLINE_SECONDS
            value['status'] = ('disabled' if not value['enabled'] else
                               'unconnected' if not value['last_heartbeat'] else
                               'online' if value['online'] else 'offline')
            value['created_at'] = value.pop('created')
            value['updated_at'] = value.pop('updated')
            items.append(value)
        return dict(items=items, queued=counts.get('queued', 0),
                    leased=counts.get('leased', 0), failed=counts.get('failed', 0))

    def _complete_collection(self, row, result):
        if row['collection_type'] == 'api':
            return self._complete_api_collection(row, result)
        if not isinstance(result, dict) or not isinstance(result.get('items'), list):
            raise CollectionError('采集节点没有返回有效的列表数据')
        created = updated = 0
        node_queued = node_skipped = node_failed = 0
        download_queued = download_skipped = download_failed = 0
        collected_ids, now = [], time.time()
        try:
            node_ids = json.loads(row['node_ids'] or '[]')
        except (TypeError, ValueError):
            node_ids = []
        try:
            sync_site_ids = json.loads(row['sync_site_ids'] or '[]')
        except (TypeError, ValueError):
            sync_site_ids = []
        for item in result['items']:
            if not isinstance(item, dict) or not item.get('id') or not item.get('url'):
                continue
            item.setdefault('source', self.collector_source(item['url']))
            task_id, was_created = self._save_collected_item(
                row['id'], item, now, bool(row['local_collect_enabled']), sync_site_ids)
            collected_ids.append(task_id)
            created += int(was_created)
            updated += int(not was_created)
            if node_ids and self.node_enqueue_callback:
                try:
                    queued = self.node_enqueue_callback(node_ids, [task_id])
                    node_queued += queued.get('queued', 0)
                    node_skipped += queued.get('skipped', 0)
                    node_failed += queued.get('failed', 0)
                except Exception:
                    node_failed += len(node_ids)
        if row['auto_download_enabled'] and collected_ids:
            try:
                scheduled = self.schedule_items(collected_ids)
                download_queued = int(scheduled.get('queued') or 0)
                download_skipped = int(scheduled.get('skipped') or 0)
                download_failed = int(scheduled.get('missing') or 0)
            except Exception:
                download_failed = len(set(collected_ids))
        if sync_site_ids and collected_ids and self.site_enqueue_callback:
            try:
                self.site_enqueue_callback(sync_site_ids, collected_ids)
            except Exception:
                log.exception('Unable to enqueue collection %s for website sync', row['id'])
        with self.db() as db:
            db.execute("""UPDATE collection_tasks SET status='idle',last_total=?,last_created=?,
                last_skipped=?,last_task_ids=?,last_node_queued=?,last_node_skipped=?,
                last_node_failed=?,last_download_queued=?,last_download_skipped=?,
                last_download_failed=?,last_updated=?,last_failed=0,last_error=NULL,last_run_at=?,updated=? WHERE id=?""",
                (len(result['items']), created, updated, json.dumps(collected_ids),
                 node_queued, node_skipped, node_failed, download_queued,
                 download_skipped, download_failed, updated, now, now, row['id']))
        self.scheduler_wake.set()
        return dict(total=len(result['items']), created=created, updated=updated,
                    task_ids=collected_ids, download_queued=download_queued,
                    download_skipped=download_skipped, download_failed=download_failed)

    def _complete_api_collection(self, row, response):
        from download_signers import SignerError
        if not isinstance(response, dict) or 'raw' not in response:
            raise CollectionError('接口采集未返回原始JSON')
        raw, source = response['raw'], row['api_source'] or None
        if isinstance(raw, list) and source:
            raw = [dict(item, source=item.get('source') or source) if isinstance(item, dict) else item for item in raw]
            source = None
        elif isinstance(raw, dict) and raw.get('source'):
            source = None
        elif isinstance(raw, dict):
            data = raw.get('data')
            candidates = raw.get('items', raw.get('list'))
            if candidates is None:
                candidates = data.get('list', data.get('items')) if isinstance(data, dict) else data
            if isinstance(candidates, list) and candidates and all(
                    isinstance(item, dict) and item.get('source') for item in candidates):
                sources = {str(item['source']).strip().lower() for item in candidates}
                if len(sources) == 1:
                    source = sources.pop()
                else:
                    raw, source = candidates, None
            elif candidates == [] and not source:
                raw = []
        try:
            items = self.downloads.signers.format_items(raw, source, allow_empty=True)
        except SignerError as exc:
            raise CollectionError(str(exc)) from None
        sync_sites = json.loads(row['sync_site_ids'] or '[]')
        # API collection follows the same two-step flow as webpage collection:
        # save the collected resources first, then let this collection task decide
        # whether those resources should enter the download queue.
        auto_download = bool(row['auto_download_enabled'])
        result = self.import_items(
            items,
            collection_id=row['id'],
            sync_site_ids=sync_sites,
            auto_download=False,
        )
        ids = list(dict.fromkeys(entry['task_id'] for entry in result['results']
            if entry.get('task_id') and (entry['status'] in ('created', 'updated') or entry.get('matched'))))
        download_queued = download_skipped = download_failed = 0
        if auto_download and ids:
            try:
                scheduled = self.schedule_items(ids)
                download_queued = int(scheduled.get('queued') or 0)
                download_skipped = int(scheduled.get('skipped') or 0)
                download_failed = int(scheduled.get('missing') or 0)
                queued_ids = set(scheduled.get('queued_ids') or [])
                for entry in result['results']:
                    if entry.get('task_id') in queued_ids:
                        entry['message'] = '等待下载'
                result['queued'] = download_queued
                result['saved_only'] = max(
                    0, int(result.get('saved_only') or 0) - download_queued)
            except Exception:
                download_failed = len(set(ids))
        if sync_sites and ids and self.site_enqueue_callback:
            try:
                self.site_enqueue_callback(sync_sites, ids)
            except Exception:
                log.exception('Unable to enqueue API collection %s for website sync', row['id'])
        errors = [f"第{item['index']}条：{item['message']}" for item in result['results'] if item['status']=='failed']
        now = time.time()
        with self.db() as db:
            db.execute("""UPDATE collection_tasks SET status='idle',last_total=?,last_created=?,
                last_updated=?,last_skipped=?,last_failed=?,last_task_ids=?,last_download_queued=?,
                last_download_skipped=?,last_download_failed=?,last_error=?,last_run_at=?,updated=? WHERE id=?""",
                (result['total'],result['created'],result['updated'],result['skipped'],result['failed'],
                 json.dumps(ids),download_queued,download_skipped,download_failed,
                 '; '.join(errors[:3])[:1000] or None,now,now,row['id']))
        self.scheduler_wake.set()
        return dict(result, task_ids=ids)

    def complete_download_resolution(self, job_id, node_id, lease_token, result):
        if not isinstance(result, dict) or not result.get('m3u8_url'):
            raise CollectionError('采集节点没有返回有效的播放地址')
        with self.prepare_lock:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                job = db.execute("""SELECT * FROM collector_jobs WHERE id=? AND status='leased'
                    AND assigned_node_id=? AND lease_token=? AND lease_until>?""",
                    (job_id, node_id, lease_token, time.time())).fetchone()
                if not job:
                    raise CollectionError('采集任务租约已失效')
                waiter = self.download_waiters.get(job['resource_id'])
                if not waiter or waiter['job_id'] != job_id:
                    raise CollectionError('该资源已释放下载名额，请重新领取任务')
                task = db.execute('SELECT * FROM tasks WHERE id=?', (job['resource_id'],)).fetchone()
                if not task or task['status'] != 'waiting_collector':
                    raise CollectionError('资源不再等待本次地址解析')
                metadata = self.downloads.merge_existing_metadata(dict(task), dict(result))
                metadata['url'] = metadata.get('url') or task['url']
                metadata['_imported'] = True
                metadata['_playback_resolved_at'] = time.time()
                self.downloads.save_metadata(db, task['id'], metadata)
                now = time.time()
                db.execute("""UPDATE tasks SET status='downloading',message='最新地址已获取，立即下载',
                              download_retry_at=0,error=NULL,updated=? WHERE id=?""", (now, task['id']))
                db.execute("""UPDATE collector_jobs SET status='completed',lease_until=NULL,
                              completed=?,updated=?,error=NULL WHERE id=?""", (now, now, job_id))
                db.execute('''UPDATE collector_nodes SET success_count=success_count+1,
                              last_heartbeat=?,updated=?,last_error=NULL WHERE id=?''', (now, now, node_id))
            waiter['result'] = metadata
            waiter['event'].set()
        return dict(task_id=task['id'], status='downloading')

    def fail_download_resolution(self, job_id, node_id, lease_token, message):
        with self.prepare_lock:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                now = time.time()
                job = db.execute("""SELECT * FROM collector_jobs WHERE id=? AND status='leased'
                    AND assigned_node_id=? AND lease_token=? AND lease_until>?""",
                    (job_id, node_id, lease_token, now)).fetchone()
                if not job:
                    return False
                waiter = self.download_waiters.get(job['resource_id'])
                if not waiter or waiter['job_id'] != job_id:
                    return False
                db.execute("""UPDATE collector_jobs SET status='failed',lease_until=NULL,error=?,
                              completed=?,updated=? WHERE id=?""", (message, now, now, job_id))
                db.execute('''UPDATE collector_nodes SET failed_count=failed_count+1,last_heartbeat=?,
                              last_error=?,updated=? WHERE id=?''', (now, message, now, node_id))
                db.execute("UPDATE tasks SET message='最新播放地址解析失败，准备重试',updated=? WHERE id=?",
                           (now, job['resource_id']))
            waiter['error'] = message
            waiter['event'].set()
        return True

    def complete_collector_job(self, job_id, node_id, lease_token, result):
        with self.db() as db:
            row = db.execute("""SELECT * FROM collector_jobs WHERE id=? AND status='leased'
                                AND assigned_node_id=? AND lease_token=? AND lease_until>?""",
                             (job_id, node_id, lease_token, time.time())).fetchone()
        if not row:
            raise CollectionError('采集任务租约已失效')
        if row['kind'] == 'detail' and row['purpose'] == 'download':
            return self.complete_download_resolution(job_id, node_id, lease_token, result)
        if row['kind'] == 'list':
            with self.db() as db:
                collection = db.execute('SELECT * FROM collection_tasks WHERE id=?',
                                        (row['collection_id'],)).fetchone()
            if not collection:
                raise CollectionError('列表采集任务已删除')
            summary = self._complete_collection(collection, result)
        elif row['kind'] == 'detail':
            if not isinstance(result, dict) or not result.get('m3u8_url'):
                raise CollectionError('采集节点没有返回有效的播放地址')
            if row['purpose'] != 'refresh':
                raise CollectionError('不支持的详情采集用途')
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                valid = db.execute("""SELECT 1 FROM collector_jobs WHERE id=? AND status='leased'
                    AND assigned_node_id=? AND lease_token=? AND lease_until>?""",
                    (job_id, node_id, lease_token, time.time())).fetchone()
                if not valid:
                    raise CollectionError('采集任务租约已失效')
                task = db.execute('SELECT * FROM tasks WHERE id=?', (row['resource_id'],)).fetchone()
                if not task:
                    raise CollectionError('资源任务已删除')
                metadata = self.downloads.merge_existing_metadata(dict(task), dict(result))
                metadata['url'] = metadata.get('url') or task['url']
                self.downloads.save_metadata(db, task['id'], metadata)
                now = time.time()
                db.execute('''UPDATE tasks SET title_key=coalesce(?,title_key),
                              assets_refresh=CASE WHEN status='completed' THEN 3 ELSE assets_refresh END,
                              message=CASE WHEN status='completed' THEN '详情已更新，等待补全预览'
                                           ELSE '详情已更新' END,updated=? WHERE id=?''',
                           (self.normalized_title(metadata.get('title')), now, task['id']))
                db.execute("""UPDATE collector_jobs SET status='completed',lease_until=NULL,
                              completed=?,updated=?,error=NULL WHERE id=?""", (now, now, job_id))
                db.execute('''UPDATE collector_nodes SET success_count=success_count+1,
                              last_heartbeat=?,updated=?,last_error=NULL WHERE id=?''', (now, now, node_id))
            if task['status'] == 'completed':
                self.downloads.wake.set()
            return dict(task_id=row['resource_id'], status='updated')
        else:
            raise CollectionError('不支持的采集任务类型')
        now = time.time()
        with self.db() as db:
            db.execute("""UPDATE collector_jobs SET status='completed',lease_until=NULL,
                          completed=?,updated=?,error=NULL WHERE id=?""", (now, now, job_id))
            db.execute('UPDATE collector_nodes SET success_count=success_count+1,last_heartbeat=?,updated=?,last_error=NULL WHERE id=?',
                       (now, now, node_id))
        return summary

    def fail_collector_job(self, job_id, node_id, lease_token, error):
        message = str(error or '本地采集失败')[:500]
        now = time.time()
        with self.db() as db:
            job = db.execute('SELECT kind,purpose FROM collector_jobs WHERE id=?', (job_id,)).fetchone()
        if job and job['kind'] == 'detail' and job['purpose'] == 'download':
            return self.fail_download_resolution(job_id, node_id, lease_token, message)
        with self.db() as db:
            row = db.execute("""SELECT * FROM collector_jobs WHERE id=? AND status='leased'
                                AND assigned_node_id=? AND lease_token=? AND lease_until>?""",
                             (job_id, node_id, lease_token, now)).fetchone()
            if not row:
                return False
            db.execute("""UPDATE collector_jobs SET status='failed',lease_until=NULL,error=?,
                          completed=?,updated=? WHERE id=?""", (message, now, now, job_id))
            db.execute('UPDATE collector_nodes SET failed_count=failed_count+1,last_heartbeat=?,last_error=?,updated=? WHERE id=?',
                       (now, message, now, node_id))
            if row['kind'] == 'list':
                db.execute("""UPDATE collection_tasks SET status='idle',last_error=?,
                              last_download_queued=0,last_download_skipped=0,
                              last_download_failed=0,last_updated=0,last_failed=1,
                              last_total=0,last_created=0,last_skipped=0,
                              last_run_at=?,updated=? WHERE id=?""",
                           (message, now, now, row['collection_id']))
            elif row['kind'] == 'detail' and row['purpose'] == 'refresh':
                db.execute("""UPDATE tasks SET message='采集客户端更新详情失败',
                              updated=? WHERE id=?""", (now, row['resource_id']))
            else:
                db.execute("""UPDATE tasks SET status='failed',message='本地采集失败',
                              error=?,updated=? WHERE id=?""", (message, now, row['resource_id']))
        return True

    def settings(self):
        with self.db() as db:
            row = db.execute('SELECT * FROM scheduler_settings WHERE id=1').fetchone()
            collection_running = db.execute('''SELECT count(*) FROM collection_tasks
                WHERE status IN ('queued','running','waiting_collector')''').fetchone()[0]
            counts = self.downloads.status_counts(db)
        return dict(enabled=bool(row['enabled']), concurrency=row['concurrency'],
                    segment_concurrency=row['segment_concurrency'],
                    collection_running=int(collection_running or 0),
                    running=self.downloads.running_count(), scheduled=counts['active'],
                    completed=counts['completed'], failed=counts['failed'], total=counts['total'],
                    download_speed=self.downloads.download_speed(), updated_at=row['updated'])

    def update_settings(self, enabled, concurrency, segment_concurrency=3):
        concurrency = max(1, min(self.downloads.MAX_CONCURRENCY, int(concurrency)))
        segment_concurrency = max(
            1, min(self.downloads.MAX_SEGMENT_CONCURRENCY, int(segment_concurrency)))
        with self.db() as db:
            db.execute('''UPDATE scheduler_settings SET enabled=?,concurrency=?,
                          segment_concurrency=?,updated=? WHERE id=1''',
                       (1 if enabled else 0, concurrency, segment_concurrency, time.time()))
        self.downloads.set_scheduler(enabled, concurrency, segment_concurrency)
        self.scheduler_wake.set()
        return self.settings()

    def list_items(self, page=1, page_size=20, title=None, source=None, status=None, collection_id=None):
        where, values = [], []
        if title:
            condition, search_values = self._resource_search_filter(title)
            if condition:
                where.append(condition)
                values.extend(search_values)
        if source:
            where.append('source=?'); values.append(source)
        if status:
            where.append('status=?'); values.append(status)
        if collection_id:
            where.append('collection_id=?'); values.append(collection_id)
        clause = (' WHERE ' + ' AND '.join(where)) if where else ''
        with self.db() as db:
            total = db.execute('SELECT count(*) FROM tasks' + clause, values).fetchone()[0]
            rows = db.execute('SELECT ' + self.ITEM_LIST_COLUMNS + ' FROM tasks' + clause
                              + ' ORDER BY last_seen DESC,id DESC LIMIT ? OFFSET ?',
                              (*values, page_size, (page - 1) * page_size)).fetchall()
            # One grouped query replaces one progress query per chapter.  Keep
            # serialization on this same connection so it never opens a
            # connection for each row.
            progress = self._item_chapter_progress(db, rows)
            items = []
            for row in rows:
                task = self.downloads.serialize_task(
                    row, db=db, chapter_progress=progress)
                pending = row['status'] == 'pending'
                items.append(dict(
                    kind='collection' if row['collection_id'] else 'download', id=row['id'],
                    collection_id=row['collection_id'], source=row['source'], video_id=row['video_id'],
                    title=row['title'], url=row['url'], cover=task['cover_url'],
                    preview_image=task['preview_url'], preview_m3u8=task['preview_m3u8_url'],
                    preview5_m3u8=task['preview5_m3u8_url'],
                    duration=row['duration'], published_at=task.get('published_at'),
                    actors=task.get('actors') or [], tags=task.get('tags') or [],
                    status=row['status'],
                    can_play=bool(task.get('m3u8_url')),
                    r2_synced=bool(row['r2_synced']),
                    r2_sync_status=row['r2_sync_status'] or ('synced' if row['r2_synced'] else 'unsynced'),
                    r2_synced_at=row['r2_synced_at'], local_deleted=bool(row['local_deleted']),
                    local_deleted_at=row['local_deleted_at'],
                    download_task_id=None if pending else row['id'], first_seen=row['first_seen'],
                    last_seen=row['last_seen'], download=None if pending else task))
        return dict(page=page, page_size=page_size, total=total, items=items)

    def _item_chapter_progress(self, db, rows):
        return self.downloads.chapter_progress(db, rows)

    def export_items(self, item_ids):
        """Return the stable public resource contract in caller-provided order."""
        unique_ids = list(dict.fromkeys(filter(None, item_ids)))
        if not unique_ids:
            return dict(items=[], missing=[])
        rows = []
        with self.db() as db:
            for offset in range(0, len(unique_ids), 500):
                chunk = unique_ids[offset:offset + 500]
                placeholders = ','.join('?' for _ in chunk)
                rows.extend(db.execute(
                    f'SELECT * FROM tasks WHERE id IN ({placeholders})', chunk).fetchall())
        by_id = {row['id']: row for row in rows}
        items = []
        for task_id in unique_ids:
            row = by_id.get(task_id)
            if not row:
                continue
            try:
                metadata = json.loads(row['metadata_json'] or '{}')
            except (ValueError, TypeError):
                metadata = {}
            poster_file = metadata.get('poster_local') or row['poster_file']
            cover_file = metadata.get('cover_local') or poster_file
            preview_file = metadata.get('preview_image_local') or row['preview_file']
            vtt_file = metadata.get('vtt_local') or row['vtt_file']
            m3u8_file = metadata.get('m3u8_local')
            preview_m3u8_file = metadata.get('preview_m3u8_local') or row['preview_m3u8_file']
            preview5_m3u8_file = metadata.get('preview5_m3u8_local') or row['preview5_m3u8_file']
            poster = (self.downloads.media_url(row['source'], row['video_id'], poster_file)
                      if poster_file else metadata.get('poster'))
            exported_chapters = []
            for index, chapter in enumerate(metadata.get('chapters', []), 1):
                if not isinstance(chapter, dict):
                    continue
                exported_chapters.append(dict(
                    index=index, title=chapter.get('title') or f'第 {index} 章',
                    duration=chapter.get('duration'),
                    m3u8_url=(self.downloads.media_url(row['source'], row['video_id'], chapter.get('m3u8_local'))
                              or chapter.get('m3u8_url')),
                    vtt_url=(self.downloads.media_url(row['source'], row['video_id'], chapter.get('vtt_local'))
                             or chapter.get('vtt_url')),
                    preview_m3u8_url=(self.downloads.media_url(
                        row['source'], row['video_id'], chapter.get('preview_m3u8_local'))
                        or chapter.get('preview_m3u8_url')),
                    preview5_m3u8_url=(self.downloads.media_url(
                        row['source'], row['video_id'], chapter.get('preview5_m3u8_local'))
                        or chapter.get('preview5_m3u8_url'))))
            items.append(dict(
                source=row['source'], id=row['video_id'], title=row['title'] or metadata.get('title'),
                url=metadata.get('url') or row['url'],
                cover=(self.downloads.media_url(row['source'], row['video_id'], cover_file)
                       or row['cover_url'] or metadata.get('cover')),
                poster=poster,
                preview_image=(self.downloads.media_url(row['source'], row['video_id'], preview_file)
                               or row['preview_url'] or metadata.get('preview_image')),
                vtt_url=(self.downloads.media_url(row['source'], row['video_id'], vtt_file)
                         or row['vtt_url'] or metadata.get('vtt_url')),
                duration=row['duration'] or metadata.get('duration'),
                preview_m3u8_url=(self.downloads.media_url(row['source'], row['video_id'], preview_m3u8_file)
                                  or row['preview_m3u8_url'] or metadata.get('preview_m3u8_url')),
                preview5_m3u8_url=(self.downloads.media_url(row['source'], row['video_id'], preview5_m3u8_file)
                                   or row['preview5_m3u8_url'] or metadata.get('preview5_m3u8_url')),
                published_at=row['release_date'] or metadata.get('published_at'),
                actors=metadata.get('actors') if isinstance(metadata.get('actors'), list) else [],
                tags=metadata.get('tags') if isinstance(metadata.get('tags'), list) else [],
                m3u8_url=(self.downloads.media_url(row['source'], row['video_id'], m3u8_file)
                          or row['m3u8_url'] or metadata.get('m3u8_url')),
                chapters=exported_chapters))
        return dict(items=items, missing=[task_id for task_id in unique_ids if task_id not in by_id])

    def refresh_details(self, item_ids, resolver):
        """Refresh remote detail metadata without changing download state or local files."""
        unique_ids = list(dict.fromkeys(filter(None, item_ids)))
        result = dict(total=len(unique_ids), updated=0, queued=0,
                      skipped=0, failed=0, results=[])
        if not unique_ids:
            return result
        rows = []
        with self.db() as db:
            for offset in range(0, len(unique_ids), 500):
                chunk = unique_ids[offset:offset + 500]
                placeholders = ','.join('?' for _ in chunk)
                rows.extend(db.execute(
                    f'SELECT * FROM tasks WHERE id IN ({placeholders})', chunk).fetchall())
        by_id = {row['id']: row for row in rows}
        allowed_statuses = {'completed', 'pending', 'failed'}
        assets_queued = False
        for item_id in unique_ids:
            row = by_id.get(item_id)
            if not row:
                result['failed'] += 1
                result['results'].append(dict(id=item_id, status='failed', message='资源不存在'))
                continue
            if row['source'] not in ('jable', '123av'):
                result['skipped'] += 1
                result['results'].append(dict(
                    id=item_id, status='skipped', message='该来源不支持重新采集详情'))
                continue
            if row['status'] not in allowed_statuses:
                result['skipped'] += 1
                result['results'].append(dict(
                    id=item_id, status='skipped', message='资源正在调度或下载，暂不更新详情'))
                continue
            if row['local_collect_enabled']:
                with self.db() as db:
                    self._enqueue_collector_job(
                        db, 'detail', row['url'], resource_id=item_id, purpose='refresh')
                    db.execute("""UPDATE tasks SET message='等待采集客户端更新详情',
                                  updated=? WHERE id=?""", (time.time(), item_id))
                result['queued'] += 1
                result['results'].append(dict(
                    id=item_id, status='queued', message='已等待采集客户端更新详情'))
                continue
            try:
                detail = resolver(row['url'])
                if not isinstance(detail, dict):
                    raise CollectionError('详情接口没有返回有效数据')
                try:
                    previous = json.loads(row['metadata_json'] or '{}')
                except (TypeError, ValueError):
                    previous = {}
                for field in ('actors', 'tags'):
                    if not detail.get(field) and previous.get(field):
                        detail[field] = previous[field]
                metadata = self.downloads.merge_existing_metadata(dict(row), detail)
                with self.db() as db:
                    self.downloads.save_metadata(db, item_id, metadata)
                    db.execute('''UPDATE tasks SET title_key=coalesce(?,title_key),
                                  url=coalesce(?,url),
                                  assets_refresh=CASE WHEN status='completed' THEN 3
                                                      ELSE assets_refresh END,
                                  message=CASE WHEN status='completed' THEN '等待补全封面和预览'
                                               ELSE message END,
                                  updated=? WHERE id=?''',
                               (self.normalized_title(metadata.get('title')),
                                metadata.get('url'), time.time(), item_id))
                assets_queued = assets_queued or row['status'] == 'completed'
                result['updated'] += 1
                result['results'].append(dict(
                    id=item_id, status='updated',
                    tags=len(metadata.get('tags') or []),
                    actors=len(metadata.get('actors') or []),
                    message=('详情已更新，附加资源已排队补全'
                             if row['status'] == 'completed' else '详情已更新')))
            except Exception as exc:
                message = (str(exc) if isinstance(exc, CollectionError)
                           else getattr(exc, 'message', None) or type(exc).__name__)
                result['failed'] += 1
                result['results'].append(dict(
                    id=item_id, status='failed', message=str(message)[:300]))
        if assets_queued:
            self.downloads.wake.set()
        return result

    def local_media(self, value):
        """Resolve public media URLs to files under the configured download root."""
        if not value:
            return None
        try:
            parts = urlsplit(value)
            if parts.netloc and parts.netloc.casefold() != urlsplit(self.downloads.public_base_url).netloc.casefold():
                return None
            path = unquote(parts.path).replace('\\', '/')
        except (TypeError, ValueError):
            return None
        roots = {
            '/uploads/': 'uploads', '/jable/': 'jable',
            '/123/': '123', '/external/': 'external',
        }
        for prefix, directory in roots.items():
            if not path.startswith(prefix):
                continue
            relative = path[len(prefix):].lstrip('/')
            public_root = (self.downloads.root / directory).resolve()
            target = (public_root / Path(relative)).resolve()
            if (not relative or public_root not in target.parents or not target.is_file()):
                if parts.netloc:
                    raise ValueError(f'本地资源不存在：{path}')
                return None
            return dict(path=target, url=f'{self.downloads.public_base_url}{path}')
        return None

    def validate_remote_paths(self, metadata):
        from download_signers import SignerError
        signers = getattr(self.downloads, 'signers', None)
        fields = {'m3u8_url': 'playlist', 'video_url': 'playlist', 'cover': 'cover',
                  'poster': 'poster', 'preview_image': 'preview_image', 'vtt_url': 'vtt',
                  'preview_m3u8_url': 'playlist', 'preview5_m3u8_url': 'playlist'}
        for resource in [metadata, *metadata.get('chapters', [])]:
            for field, kind in fields.items():
                value = resource.get(field)
                if not value or urlsplit(value).scheme in ('http', 'https') or self.local_media(value):
                    continue
                if not signers:
                    raise ValueError(f'{field} 是相对路径，请先在签名管理配置来源域名')
                try:
                    signers.resolve_url(metadata['source'], value, kind)
                except SignerError as exc:
                    raise ValueError(str(exc)) from None

    def validate_import_items(self, items, auto_download=True):
        """Preview validation and duplicate handling without creating tasks."""
        from resource_import import validate_resource
        results, seen_ids, seen_titles = [], set(), set()
        local_linked = queued = updated = skipped = failed = saved_only = 0
        with self.db() as db:
            for index, item in enumerate(items, 1):
                try:
                    metadata = validate_resource(item)
                    local = self.local_media(
                        metadata.get('m3u8_url') or metadata.get('video_url') or metadata.get('url'))
                    if not local:
                        self.validate_remote_paths(metadata)
                except ValueError as exc:
                    failed += 1
                    results.append(dict(index=index, status='failed', message=str(exc)))
                    continue
                title_key = self.normalized_title(metadata['title'])
                video_key = metadata['id'].strip().casefold()
                if video_key in seen_ids or title_key in seen_titles:
                    skipped += 1
                    results.append(dict(index=index, status='skipped',
                                        message='本次数据中番号或标题重复，已跳过'))
                    continue
                seen_ids.add(video_key); seen_titles.add(title_key)
                existing = db.execute('''SELECT id,status FROM tasks
                    WHERE lower(trim(video_id))=lower(trim(?)) LIMIT 1''',
                    (metadata['id'],)).fetchone()
                if existing:
                    title_conflict = db.execute('''SELECT id FROM tasks
                        WHERE title_key=? AND id<>? LIMIT 1''',
                        (title_key, existing['id'])).fetchone()
                    if existing['status'] not in ('pending', 'failed') or title_conflict:
                        skipped += 1
                        message = ('新标题与其他资源重复，将跳过覆盖' if title_conflict
                                   else f'任务状态为 {existing["status"]}，将跳过覆盖')
                        results.append(dict(index=index, status='skipped',
                                            task_id=existing['id'], message=message))
                        continue
                    updated += 1
                    if local:
                        local_linked += 1
                        message = '校验通过，将覆盖旧任务并关联本地资源'
                    elif auto_download:
                        queued += 1
                        message = '校验通过，将覆盖旧任务并重新排队'
                    else:
                        saved_only += 1
                        message = '校验通过，仅保存数据，等待手动加入下载'
                    results.append(dict(index=index, status='valid',
                                        task_id=existing['id'], message=message, action='update'))
                    continue
                title_duplicate = db.execute(
                    'SELECT id FROM tasks WHERE title_key=? LIMIT 1', (title_key,)).fetchone()
                if title_duplicate:
                    skipped += 1
                    results.append(dict(index=index, status='skipped', task_id=title_duplicate['id'],
                                        message='标题已存在但番号不同，将跳过'))
                    continue
                if local:
                    local_linked += 1
                    message = '校验通过，本地资源将直接关联'
                elif auto_download:
                    queued += 1
                    message = '校验通过，远程资源将加入下载队列'
                else:
                    saved_only += 1
                    message = '校验通过，仅保存数据，等待手动加入下载'
                results.append(dict(index=index, status='valid', message=message))
        return dict(total=len(items), valid=local_linked + queued + saved_only, updated=updated,
                    saved_only=saved_only,
                    local_linked=local_linked, queued=queued, skipped=skipped,
                    failed=failed, results=results)

    def import_items(self, items, collection_id=None, sync_site_ids=None, auto_download=True):
        from resource_import import validate_resource
        results, created_ids, updated_ids, queued_ids = [], [], [], []
        local_linked = saved_only = 0
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for index, item in enumerate(items, 1):
                try:
                    metadata = validate_resource(item)
                    local = self.local_media(
                        metadata.get('m3u8_url') or metadata.get('video_url') or metadata.get('url'))
                    if not local:
                        self.validate_remote_paths(metadata)
                except ValueError as exc:
                    results.append(dict(index=index, status='failed', message=str(exc)))
                    continue
                title_key = self.normalized_title(metadata['title'])
                existing = db.execute('''SELECT * FROM tasks
                    WHERE lower(trim(video_id))=lower(trim(?)) LIMIT 1''',
                    (metadata['id'],)).fetchone()
                if existing and existing['status'] in ('pending', 'failed'):
                    title_conflict = db.execute('''SELECT id FROM tasks
                        WHERE title_key=? AND id<>? LIMIT 1''',
                        (title_key, existing['id'])).fetchone()
                    with self.downloads.active_condition:
                        active = existing['id'] in self.downloads.active_tasks
                    if title_conflict:
                        results.append(dict(index=index, status='skipped', task_id=existing['id'],
                                            message='新标题与其他资源重复，已跳过覆盖'))
                        continue
                    if active:
                        results.append(dict(index=index, status='skipped', task_id=existing['id'],
                                            message='任务仍在执行中，已跳过覆盖'))
                        continue
                    folder = self.downloads.task_folder(existing['source'], existing['video_id'])
                    try:
                        if folder.exists():
                            shutil.rmtree(folder)
                    except OSError:
                        results.append(dict(index=index, status='failed', task_id=existing['id'],
                                            message='旧下载文件清理失败，请稍后重试'))
                        continue
                    now = time.time()
                    status, message = (('completed', '本地资源已关联') if local else
                        ('queued', '等待下载') if auto_download else
                        ('pending', '已保存资源，等待手动加入下载'))
                    if local:
                        metadata['m3u8_url'] = local['url']
                        metadata['_local_resource'] = True
                        local_linked += 1
                    elif auto_download:
                        queued_ids.append(existing['id'])
                    else:
                        saved_only += 1
                    db.execute('DELETE FROM files WHERE task_id=?', (existing['id'],))
                    db.execute('''UPDATE tasks SET source=?,video_id=?,title=?,title_key=?,url=?,
                        release_date=NULL,duration=NULL,cover_url=NULL,poster_file=NULL,
                        preview_url=NULL,preview_file=NULL,vtt_url=NULL,vtt_file=NULL,
                        m3u8_url=NULL,preview_m3u8_url=NULL,preview_m3u8_file=NULL,
                        preview5_m3u8_url=NULL,preview5_m3u8_file=NULL,metadata_json='{}',
                        status=?,message=?,error=NULL,warnings='[]',plan=NULL,assets_refresh=0,
                        total_segments=0,downloaded_segments=0,downloaded_bytes=0,
                        last_seen=?,updated=? WHERE id=?''',
                               (metadata['source'], metadata['id'], metadata['title'], title_key,
                                metadata['url'] or metadata['m3u8_url'], status, message,
                                now, now, existing['id']))
                    self.downloads.save_metadata(db, existing['id'], metadata)
                    updated_ids.append(existing['id'])
                    results.append(dict(index=index, status='updated', task_id=existing['id'],
                                        message='旧资源已覆盖' + ('并重新加入下载队列' if status == 'queued' else
                                            '，仅保存数据' if status == 'pending' else '')))
                    continue
                if existing:
                    results.append(dict(index=index, status='skipped', task_id=existing['id'],
                                        matched=True, message=f'任务状态为 {existing["status"]}，已跳过覆盖'))
                    continue
                title_duplicate = db.execute(
                    'SELECT id FROM tasks WHERE title_key=? LIMIT 1', (title_key,)).fetchone()
                if title_duplicate:
                    results.append(dict(index=index, status='skipped', task_id=title_duplicate['id'],
                                        message='标题已存在但番号不同，已跳过'))
                    continue
                now, task_id = time.time(), uuid.uuid4().hex
                if local:
                    metadata['m3u8_url'] = local['url']
                    metadata['_local_resource'] = True
                    status, message = 'completed', '本地资源已关联'
                    local_linked += 1
                elif auto_download:
                    status, message = 'queued', '等待下载'
                    queued_ids.append(task_id)
                else:
                    status, message = 'pending', '已保存资源，等待手动加入下载'
                    saved_only += 1
                db.execute('''INSERT INTO tasks(id,source,video_id,title,title_key,url,status,message,
                    first_seen,last_seen,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (task_id, metadata['source'], metadata['id'], metadata['title'], title_key,
                     metadata['url'] or metadata['m3u8_url'], status, message,
                     now, now, now, now))
                self.downloads.save_metadata(db, task_id, metadata)
                created_ids.append(task_id)
                results.append(dict(index=index, status='created', task_id=task_id, message=message))
            if collection_id:
                for entry in results:
                    task_id = entry.get('task_id')
                    if not task_id or not (entry['status'] in ('created', 'updated') or entry.get('matched')):
                        continue
                    existing = db.execute('SELECT sync_site_ids FROM tasks WHERE id=?',(task_id,)).fetchone()
                    merged_sites = list(dict.fromkeys(json.loads(existing['sync_site_ids'] or '[]') + (sync_site_ids or [])))
                    db.execute('UPDATE tasks SET collection_id=coalesce(collection_id,?),sync_site_ids=? WHERE id=?',
                               (collection_id,json.dumps(merged_sites),task_id))
                    if entry['status'] in ('created','updated'):
                        db.execute('UPDATE tasks SET local_collect_enabled=0 WHERE id=?',(task_id,))
        if queued_ids:
            self.downloads.wake.set()
        return dict(total=len(items), created=len(created_ids),
                    updated=len(updated_ids),
                    skipped=sum(item['status'] == 'skipped' for item in results),
                    failed=sum(item['status'] == 'failed' for item in results),
                    local_linked=local_linked, queued=len(queued_ids), saved_only=saved_only, results=results)

    def schedule_item(self, item_id, retry_failed=True):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,url,local_collect_enabled FROM tasks WHERE id=?',
                             (item_id,)).fetchone()
            if not row:
                return None
            status = row['status']
            if row['status'] in ('pending', 'failed'):
                if row['local_collect_enabled']:
                    status, message = 'queued', '等待下载调度，轮到任务时代理解析最新地址'
                else:
                    status, message = 'queued', '等待下载'
                db.execute('UPDATE tasks SET status=?,message=?,error=NULL,download_retry_at=0,updated=? WHERE id=?',
                           (status, message, time.time(), item_id))
        self.downloads.wake.set(); self.scheduler_wake.set()
        return dict(id=item_id, status=status, download_task_id=item_id)

    def schedule_items(self, item_ids):
        queued, skipped, missing = [], [], []
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for item_id in dict.fromkeys(item_ids):
                row = db.execute('SELECT status,url,local_collect_enabled FROM tasks WHERE id=?',
                                 (item_id,)).fetchone()
                if not row:
                    missing.append(item_id)
                elif row['status'] not in ('pending', 'failed'):
                    skipped.append(item_id)
                else:
                    if row['local_collect_enabled']:
                        status, message = 'queued', '等待下载调度，轮到任务时代理解析最新地址'
                    else:
                        status, message = 'queued', '等待下载'
                    db.execute('UPDATE tasks SET status=?,message=?,error=NULL,download_retry_at=0,updated=? WHERE id=?',
                               (status, message, time.time(), item_id))
                    queued.append(item_id)
        if queued:
            self.downloads.wake.set(); self.scheduler_wake.set()
        return dict(queued=len(queued), skipped=len(skipped), missing=len(missing), queued_ids=queued)

    def enqueue_all_pending(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT id,url,local_collect_enabled FROM tasks WHERE status='pending'").fetchall()
            now = time.time()
            for row in rows:
                if row['local_collect_enabled']:
                    status, message = 'queued', '等待下载调度，轮到任务时代理解析最新地址'
                else:
                    status, message = 'queued', '等待下载'
                db.execute('UPDATE tasks SET status=?,message=?,error=NULL,download_retry_at=0,updated=? WHERE id=?',
                           (status, message, now, row['id']))
        if rows:
            self.downloads.wake.set(); self.scheduler_wake.set()
        return dict(total=len(rows), queued=len(rows), skipped=0, reused=0)

    def queue_due_collections(self):
        local_now = datetime.now(ZoneInfo('Asia/Shanghai'))
        if local_now.hour < 18:
            return
        today = local_now.date().isoformat()
        queued = False
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('''SELECT id,status FROM collection_tasks
                                 WHERE scheduled_enabled=1 AND coalesce(last_scheduled_date,'')<>?''', (today,)).fetchall()
            for row in rows:
                if row['status'] == 'idle':
                    db.execute("""UPDATE collection_tasks SET status='queued',last_error=NULL,
                                  last_scheduled_date=?,updated=? WHERE id=?""", (today, time.time(), row['id']))
                    queued = True
                else:
                    db.execute('UPDATE collection_tasks SET last_scheduled_date=?,updated=? WHERE id=?',
                               (today, time.time(), row['id']))
        if queued:
            self.wake.set()

    def close(self):
        self.stop_event.set(); self.wake.set(); self.scheduler_wake.set()
        if self.thread.is_alive():
            self.thread.join(timeout=35)
        if self.scheduler_thread.is_alive():
            self.scheduler_thread.join(timeout=5)

    def _save_collected_item(self, collection_id, item, now, local_collect_enabled=False,
                             sync_site_ids=None):
        title_key = self.normalized_title(item.get('title'))
        sync_site_ids_json = json.dumps(list(dict.fromkeys(sync_site_ids or [])))
        with self.db() as db:
            existing = db.execute('''SELECT * FROM tasks WHERE lower(trim(video_id))=lower(trim(?))
                                     LIMIT 1''', (item['id'],)).fetchone()
            if not existing and title_key:
                existing = db.execute('SELECT * FROM tasks WHERE title_key=? LIMIT 1',
                                      (title_key,)).fetchone()
            if existing:
                same_code = existing['video_id'].strip().casefold() == item['id'].strip().casefold()
                if same_code:
                    db.execute('''UPDATE tasks SET collection_id=?,source=?,title=?,title_key=?,url=?,
                        sync_site_ids=?,
                        local_collect_enabled=CASE WHEN ?=1 THEN 1 ELSE local_collect_enabled END,
                        cover_url=CASE WHEN cover_url LIKE '/api/download/%' THEN cover_url ELSE coalesce(?,cover_url) END,
                        preview_url=CASE WHEN preview_url LIKE '/api/download/%' THEN preview_url ELSE coalesce(?,preview_url) END,
                        duration=coalesce(?,duration),last_seen=?,updated=? WHERE id=?''',
                        (collection_id, item['source'], item.get('title'), title_key, item['url'],
                         sync_site_ids_json,
                         1 if local_collect_enabled else 0,
                         item.get('cover'), item.get('preview_image'), item.get('duration'),
                         now, now, existing['id']))
                else:
                    db.execute('''UPDATE tasks SET collection_id=?,sync_site_ids=?,last_seen=?,updated=?,
                        local_collect_enabled=CASE WHEN ?=1 THEN 1 ELSE local_collect_enabled END,
                        cover_url=CASE WHEN cover_url LIKE '/api/download/%' THEN cover_url ELSE coalesce(cover_url,?) END,
                        preview_url=CASE WHEN preview_url LIKE '/api/download/%' THEN preview_url ELSE coalesce(preview_url,?) END,
                        duration=coalesce(duration,?) WHERE id=?''',
                        (collection_id, sync_site_ids_json, now, now,
                         1 if local_collect_enabled else 0,
                         item.get('cover'), item.get('preview_image'),
                         item.get('duration'), existing['id']))
                return existing['id'], False
            task_id = uuid.uuid4().hex
            db.execute('''INSERT INTO tasks(id,collection_id,source,video_id,title,title_key,url,
                cover_url,preview_url,duration,local_collect_enabled,sync_site_ids,status,message,
                first_seen,last_seen,created,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending','等待加入下载',?,?,?,?)''',
                (task_id, collection_id, item['source'], item['id'], item.get('title'), title_key,
                 item['url'], item.get('cover'), item.get('preview_image'), item.get('duration'),
                 1 if local_collect_enabled else 0, sync_site_ids_json, now, now, now, now))
            return task_id, True

    def run_collection(self):
        while not self.stop_event.is_set():
            with self.db() as db:
                row = db.execute("SELECT * FROM collection_tasks WHERE status='queued' ORDER BY updated LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE collection_tasks SET status='running',updated=? WHERE id=?", (time.time(), row['id']))
            if not row:
                self.wake.wait(1); self.wake.clear(); continue
            created = updated = 0
            node_queued = node_skipped = node_failed = 0
            download_queued = download_skipped = download_failed = 0
            collected_ids = []
            try:
                if row['local_collect_enabled']:
                    with self.db() as db:
                        self._enqueue_collector_job(db, 'list', row['url'], collection_id=row['id'])
                        db.execute("""UPDATE collection_tasks SET status='waiting_collector',
                                      last_error=NULL,updated=? WHERE id=?""", (time.time(), row['id']))
                    continue
                if row['collection_type'] == 'api':
                    from api_collection import fetch_api_json, ApiCollectionError
                    try:
                        result = fetch_api_json(row['url'], self.serialize(row))
                    except ApiCollectionError as exc:
                        raise CollectionError(str(exc)) from None
                else:
                    result = self.collect(row['url'])
                self._complete_collection(row, result)
            except Exception as exc:
                message = str(exc) if isinstance(exc, CollectionError) else getattr(exc, 'message', None) or type(exc).__name__
                with self.db() as db:
                    failed_at = time.time()
                    db.execute('UPDATE collection_tasks SET last_updated=0,last_failed=1 WHERE id=?',(row['id'],))
                    db.execute('''UPDATE collection_tasks SET status='idle',last_total=?,last_created=?,
                        last_skipped=?,last_task_ids=?,last_node_queued=?,last_node_skipped=?,
                        last_node_failed=?,last_download_queued=?,last_download_skipped=?,
                        last_download_failed=?,last_error=?,last_run_at=?,updated=? WHERE id=?''',
                        (len(collected_ids), created, updated, json.dumps(collected_ids),
                         node_queued, node_skipped, node_failed, download_queued,
                         download_skipped, download_failed, message[:300], failed_at,
                         failed_at, row['id']))

    def run_scheduler(self):
        while not self.stop_event.is_set():
            try:
                self.queue_due_collections()
                settings = self.settings()
                self.downloads.set_scheduler(settings['enabled'], settings['concurrency'],
                                             settings['segment_concurrency'])
            except Exception:
                pass
            self.scheduler_wake.wait(1); self.scheduler_wake.clear()
