"""Persistent list collection and download scheduling on the unified tasks table."""
import json
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


class CollectionError(Exception):
    pass


class CollectionManager:
    def __init__(self, root, downloads, collect):
        self.database = downloads.database
        self.downloads = downloads
        self.collect = collect
        self.stop_event, self.wake, self.scheduler_wake = threading.Event(), threading.Event(), threading.Event()
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS collection_tasks (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
                  batch_size INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'idle',
                  scheduled_enabled INTEGER NOT NULL DEFAULT 0, last_scheduled_date TEXT,
                  last_total INTEGER NOT NULL DEFAULT 0, last_created INTEGER NOT NULL DEFAULT 0,
                  last_skipped INTEGER NOT NULL DEFAULT 0, last_task_ids TEXT NOT NULL DEFAULT '[]',
                  last_error TEXT, last_run_at REAL, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS scheduler_settings (
                  id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 1,
                  concurrency INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO scheduler_settings(id,enabled,concurrency,updated) VALUES(1,1,1,?)',
                       (time.time(),))
            db.execute("""UPDATE collection_tasks SET status='idle',last_error='服务重启，采集已中断'
                          WHERE status IN ('queued','running')""")
        initial_settings = self.settings()
        self.downloads.set_scheduler(initial_settings['enabled'], initial_settings['concurrency'])
        self.thread = threading.Thread(target=self.run_collection, name='collection-worker', daemon=True)
        self.scheduler_thread = threading.Thread(target=self.run_scheduler, name='resource-scheduler', daemon=True)
        self.thread.start(); self.scheduler_thread.start()

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
    def serialize(row):
        if not row:
            return None
        value = dict(row)
        if 'last_task_ids' in value:
            value['last_task_ids'] = json.loads(value['last_task_ids'] or '[]')
        if 'created' in value:
            value['created_at'] = value.pop('created')
        if 'updated' in value:
            value['updated_at'] = value.pop('updated')
        if 'scheduled_enabled' in value:
            value['scheduled_enabled'] = bool(value['scheduled_enabled'])
        return value

    @staticmethod
    def normalized_title(title):
        if not title:
            return None
        value = ' '.join(unicodedata.normalize('NFKC', title).split()).casefold()
        return value or None

    def create(self, name, url, batch_size=1, scheduled_enabled=False):
        now, task_id = time.time(), uuid.uuid4().hex
        with self.db() as db:
            db.execute('''INSERT INTO collection_tasks(id,name,url,batch_size,scheduled_enabled,created,updated)
                          VALUES(?,?,?,?,?,?,?)''',
                       (task_id, name, url, 1, 1 if scheduled_enabled else 0, now, now))
        return self.get(task_id)

    def create_many(self, tasks, scheduled_enabled=False):
        """Create collection tasks in one transaction and skip duplicate names or URLs."""
        created, skipped, results = [], [], []
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing_rows = db.execute('SELECT name,url FROM collection_tasks').fetchall()
            names = {self.normalized_title(row['name']) for row in existing_rows}
            urls = {row['url'] for row in existing_rows}
            for task in tasks:
                name, url = task['name'], task['url']
                name_key = self.normalized_title(name)
                if name_key in names or url in urls:
                    skipped.append(dict(page=task.get('page'), name=name, url=url))
                    results.append(dict(page=task.get('page'), status='skipped',
                                        name=name, url=url, message='标题或链接已存在'))
                    continue
                task_id = uuid.uuid4().hex
                db.execute('''INSERT INTO collection_tasks(
                    id,name,url,batch_size,scheduled_enabled,created,updated)
                    VALUES(?,?,?,?,?,?,?)''',
                           (task_id, name, url, 1, 1 if scheduled_enabled else 0, now, now))
                value = dict(id=task_id, page=task.get('page'), name=name, url=url)
                created.append(value)
                results.append(dict(**value, status='created', message='创建成功'))
                names.add(name_key)
                urls.add(url)
        return dict(total=len(tasks), created=len(created), skipped=len(skipped),
                    created_tasks=created, skipped_tasks=skipped, results=results)

    def get(self, task_id):
        with self.db() as db:
            return self.serialize(db.execute('SELECT * FROM collection_tasks WHERE id=?', (task_id,)).fetchone())

    def list(self):
        with self.db() as db:
            return [self.serialize(row) for row in db.execute('SELECT * FROM collection_tasks ORDER BY created DESC')]

    def update(self, task_id, name, url, batch_size=1, scheduled_enabled=False):
        with self.db() as db:
            row = db.execute('SELECT status FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                return None
            if row['status'] in ('queued', 'running'):
                raise CollectionError('任务执行中，暂时不能修改')
            db.execute('''UPDATE collection_tasks SET name=?,url=?,batch_size=1,scheduled_enabled=?,updated=?
                          WHERE id=?''', (name, url, 1 if scheduled_enabled else 0, time.time(), task_id))
        return self.get(task_id)

    def delete(self, task_id):
        with self.db() as db:
            row = db.execute('SELECT status FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                return False
            if row['status'] in ('queued', 'running'):
                raise CollectionError('任务执行中，暂时不能删除')
            db.execute('UPDATE tasks SET collection_id=NULL WHERE collection_id=?', (task_id,))
            db.execute('DELETE FROM collection_tasks WHERE id=?', (task_id,))
        return True

    def execute(self, task_id):
        with self.db() as db:
            row = db.execute('SELECT status FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                return None
            if row['status'] in ('queued', 'running'):
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
                elif row['status'] in ('queued', 'running'):
                    skipped.append(task_id)
                else:
                    db.execute("UPDATE collection_tasks SET status='queued',last_error=NULL,updated=? WHERE id=?",
                               (time.time(), task_id))
                    queued.append(task_id)
        if queued:
            self.wake.set()
        return dict(queued=len(queued), skipped=len(skipped), missing=len(missing), queued_ids=queued)

    def settings(self):
        with self.db() as db:
            row = db.execute('SELECT * FROM scheduler_settings WHERE id=1').fetchone()
        counts = self.downloads.status_counts()
        return dict(enabled=bool(row['enabled']), concurrency=row['concurrency'],
                    running=self.downloads.running_count(), scheduled=counts['active'],
                    completed=counts['completed'], failed=counts['failed'], total=counts['total'],
                    download_speed=self.downloads.download_speed(), updated_at=row['updated'])

    def update_settings(self, enabled, concurrency):
        concurrency = max(1, min(self.downloads.MAX_CONCURRENCY, int(concurrency)))
        with self.db() as db:
            db.execute('UPDATE scheduler_settings SET enabled=?,concurrency=?,updated=? WHERE id=1',
                       (1 if enabled else 0, concurrency, time.time()))
        self.downloads.set_scheduler(enabled, concurrency)
        self.scheduler_wake.set()
        return self.settings()

    def list_items(self, page=1, page_size=20, title=None, source=None, status=None, collection_id=None):
        where, values = [], []
        if title:
            value = '%' + title + '%'
            where.append('(title LIKE ? OR video_id LIKE ?)'); values.extend((value, value))
        if source:
            where.append('source=?'); values.append(source)
        if status:
            where.append('status=?'); values.append(status)
        if collection_id:
            where.append('collection_id=?'); values.append(collection_id)
        clause = (' WHERE ' + ' AND '.join(where)) if where else ''
        with self.db() as db:
            total = db.execute('SELECT count(*) FROM tasks' + clause, values).fetchone()[0]
            rows = db.execute('SELECT * FROM tasks' + clause + ' ORDER BY last_seen DESC LIMIT ? OFFSET ?',
                              (*values, page_size, (page - 1) * page_size)).fetchall()
        items = []
        for row in rows:
            task = self.downloads.serialize_task(row)
            pending = row['status'] == 'pending'
            items.append(dict(
                kind='collection' if row['collection_id'] else 'download', id=row['id'],
                collection_id=row['collection_id'], source=row['source'], video_id=row['video_id'],
                title=row['title'], url=row['url'], cover=row['cover_url'],
                preview_image=row['preview_url'], preview_m3u8=row['preview_m3u8_url'],
                preview5_m3u8=row['preview5_m3u8_url'],
                duration=row['duration'], status=row['status'],
                download_task_id=None if pending else row['id'], first_seen=row['first_seen'],
                last_seen=row['last_seen'], download=None if pending else task))
        return dict(page=page, page_size=page_size, total=total, items=items)

    def export_items(self, item_ids):
        """Return the stable public resource contract in caller-provided order."""
        unique_ids = list(dict.fromkeys(filter(None, item_ids)))
        if not unique_ids:
            return dict(items=[], missing=[])
        placeholders = ','.join('?' for _ in unique_ids)
        with self.db() as db:
            rows = db.execute(f'SELECT * FROM tasks WHERE id IN ({placeholders})', unique_ids).fetchall()
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
            poster = (self.downloads.media_url(row['source'], row['video_id'], row['poster_file'])
                      if row['poster_file'] else metadata.get('poster'))
            items.append(dict(
                source=row['source'], id=row['video_id'], title=row['title'] or metadata.get('title'),
                url=metadata.get('url') or row['url'], cover=row['cover_url'] or metadata.get('cover'),
                poster=poster, preview_image=row['preview_url'] or metadata.get('preview_image'),
                vtt_url=row['vtt_url'] or metadata.get('vtt_url'), duration=row['duration'] or metadata.get('duration'),
                preview_m3u8_url=row['preview_m3u8_url'] or metadata.get('preview_m3u8_url'),
                preview5_m3u8_url=row['preview5_m3u8_url'] or metadata.get('preview5_m3u8_url'),
                published_at=row['release_date'] or metadata.get('published_at'),
                actors=metadata.get('actors') if isinstance(metadata.get('actors'), list) else [],
                tags=metadata.get('tags') if isinstance(metadata.get('tags'), list) else [],
                m3u8_url=row['m3u8_url'] or metadata.get('m3u8_url')))
        return dict(items=items, missing=[task_id for task_id in unique_ids if task_id not in by_id])

    def local_media(self, value):
        """Resolve public media URLs to files under the configured download root."""
        if not value:
            return None
        try:
            path = unquote(urlsplit(value).path).replace('\\', '/')
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
                raise ValueError(f'本地资源不存在：{path}')
            return dict(path=target, url=f'{self.downloads.public_base_url}{path}')
        return None

    def validate_import_items(self, items):
        """Preview validation and duplicate handling without creating tasks."""
        from resource_import import validate_resource
        results, seen_ids, seen_titles = [], set(), set()
        local_linked = queued = skipped = failed = 0
        with self.db() as db:
            for index, item in enumerate(items, 1):
                try:
                    metadata = validate_resource(item)
                    local = self.local_media(
                        metadata.get('m3u8_url') or metadata.get('video_url') or metadata.get('url'))
                except ValueError as exc:
                    failed += 1
                    results.append(dict(index=index, status='failed', message=str(exc)))
                    continue
                title_key = self.normalized_title(metadata['title'])
                video_key = metadata['id'].strip().casefold()
                existing = db.execute('''SELECT id FROM tasks
                    WHERE lower(trim(video_id))=lower(trim(?)) OR title_key=? LIMIT 1''',
                    (metadata['id'], title_key)).fetchone()
                if existing or video_key in seen_ids or title_key in seen_titles:
                    skipped += 1
                    result = dict(index=index, status='skipped', message='番号或标题已存在，导入时会跳过')
                    if existing:
                        result['task_id'] = existing['id']
                    results.append(result)
                    continue
                seen_ids.add(video_key); seen_titles.add(title_key)
                if local:
                    local_linked += 1
                    message = '校验通过，本地资源将直接关联'
                else:
                    queued += 1
                    message = '校验通过，远程资源将加入下载队列'
                results.append(dict(index=index, status='valid', message=message))
        return dict(total=len(items), valid=local_linked + queued, local_linked=local_linked,
                    queued=queued, skipped=skipped, failed=failed, results=results)

    def import_items(self, items):
        from resource_import import validate_resource
        results, created_ids, queued_ids = [], [], []
        local_linked = 0
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for index, item in enumerate(items, 1):
                try:
                    metadata = validate_resource(item)
                    local = self.local_media(
                        metadata.get('m3u8_url') or metadata.get('video_url') or metadata.get('url'))
                except ValueError as exc:
                    results.append(dict(index=index, status='failed', message=str(exc)))
                    continue
                title_key = self.normalized_title(metadata['title'])
                existing = db.execute('''SELECT id FROM tasks
                    WHERE lower(trim(video_id))=lower(trim(?)) OR title_key=? LIMIT 1''',
                    (metadata['id'], title_key)).fetchone()
                if existing:
                    results.append(dict(index=index, status='skipped', task_id=existing['id'],
                                        message='番号或标题已存在，已跳过'))
                    continue
                now, task_id = time.time(), uuid.uuid4().hex
                if local:
                    metadata['m3u8_url'] = local['url']
                    metadata['_local_resource'] = True
                    status, message = 'completed', '本地资源已关联'
                    local_linked += 1
                else:
                    status, message = 'queued', '等待下载'
                    queued_ids.append(task_id)
                db.execute('''INSERT INTO tasks(id,source,video_id,title,title_key,url,status,message,
                    first_seen,last_seen,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (task_id, metadata['source'], metadata['id'], metadata['title'], title_key,
                     metadata['url'] or metadata['m3u8_url'], status, message,
                     now, now, now, now))
                self.downloads.save_metadata(db, task_id, metadata)
                created_ids.append(task_id)
                results.append(dict(index=index, status='created', task_id=task_id, message=message))
        if queued_ids:
            self.downloads.wake.set()
        return dict(total=len(items), created=len(created_ids),
                    skipped=sum(item['status'] == 'skipped' for item in results),
                    failed=sum(item['status'] == 'failed' for item in results),
                    local_linked=local_linked, queued=len(queued_ids), results=results)

    def schedule_item(self, item_id, retry_failed=True):
        with self.db() as db:
            row = db.execute('SELECT status FROM tasks WHERE id=?', (item_id,)).fetchone()
            if not row:
                return None
            if row['status'] in ('pending', 'failed'):
                db.execute("UPDATE tasks SET status='queued',message='等待下载',error=NULL,updated=? WHERE id=?",
                           (time.time(), item_id))
        self.downloads.wake.set(); self.scheduler_wake.set()
        return dict(id=item_id, status='queued', download_task_id=item_id)

    def schedule_items(self, item_ids):
        queued, skipped, missing = [], [], []
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for item_id in dict.fromkeys(item_ids):
                row = db.execute('SELECT status FROM tasks WHERE id=?', (item_id,)).fetchone()
                if not row:
                    missing.append(item_id)
                elif row['status'] not in ('pending', 'failed'):
                    skipped.append(item_id)
                else:
                    db.execute("UPDATE tasks SET status='queued',message='等待下载',error=NULL,updated=? WHERE id=?",
                               (time.time(), item_id))
                    queued.append(item_id)
        if queued:
            self.downloads.wake.set(); self.scheduler_wake.set()
        return dict(queued=len(queued), skipped=len(skipped), missing=len(missing), queued_ids=queued)

    def enqueue_all_pending(self):
        with self.db() as db:
            rows = db.execute("SELECT id FROM tasks WHERE status='pending'").fetchall()
            db.execute("UPDATE tasks SET status='queued',message='等待下载',error=NULL,updated=? WHERE status='pending'",
                       (time.time(),))
        if rows:
            self.downloads.wake.set(); self.scheduler_wake.set()
        return dict(total=len(rows), queued=len(rows), reused=0)

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
        self.thread.join(timeout=35); self.scheduler_thread.join(timeout=5)

    def run_collection(self):
        while not self.stop_event.is_set():
            with self.db() as db:
                row = db.execute("SELECT * FROM collection_tasks WHERE status='queued' ORDER BY updated LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE collection_tasks SET status='running',updated=? WHERE id=?", (time.time(), row['id']))
            if not row:
                self.wake.wait(1); self.wake.clear(); continue
            created = updated = 0
            collected_ids = []
            try:
                result = self.collect(row['url'])
                now = time.time()
                with self.db() as db:
                    for item in result['items']:
                        title_key = self.normalized_title(item.get('title'))
                        existing = db.execute('''SELECT * FROM tasks WHERE lower(trim(video_id))=lower(trim(?))
                                                 LIMIT 1''', (item['id'],)).fetchone()
                        if not existing and title_key:
                            existing = db.execute('SELECT * FROM tasks WHERE title_key=? LIMIT 1',
                                                  (title_key,)).fetchone()
                        if existing:
                            same_code = existing['video_id'].strip().casefold() == item['id'].strip().casefold()
                            if same_code:
                                db.execute('''UPDATE tasks SET collection_id=?,source=?,title=?,title_key=?,url=?,
                                    cover_url=CASE WHEN cover_url LIKE '/api/download/%' THEN cover_url ELSE coalesce(?,cover_url) END,
                                    preview_url=CASE WHEN preview_url LIKE '/api/download/%' THEN preview_url ELSE coalesce(?,preview_url) END,
                                    duration=coalesce(?,duration),last_seen=?,updated=? WHERE id=?''',
                                    (row['id'], item['source'], item.get('title'), title_key, item['url'],
                                     item.get('cover'), item.get('preview_image'), item.get('duration'),
                                     now, now, existing['id']))
                            else:
                                db.execute('''UPDATE tasks SET collection_id=?,last_seen=?,updated=?,
                                    cover_url=CASE WHEN cover_url LIKE '/api/download/%' THEN cover_url ELSE coalesce(cover_url,?) END,
                                    preview_url=CASE WHEN preview_url LIKE '/api/download/%' THEN preview_url ELSE coalesce(preview_url,?) END,
                                    duration=coalesce(duration,?) WHERE id=?''',
                                    (row['id'], now, now, item.get('cover'), item.get('preview_image'),
                                     item.get('duration'), existing['id']))
                            collected_ids.append(existing['id']); updated += 1
                        else:
                            task_id = uuid.uuid4().hex
                            db.execute('''INSERT INTO tasks(id,collection_id,source,video_id,title,title_key,url,
                                cover_url,preview_url,duration,status,message,first_seen,last_seen,created,updated)
                                VALUES(?,?,?,?,?,?,?,?,?,?,'pending','等待加入下载',?,?,?,?)''',
                                (task_id, row['id'], item['source'], item['id'], item.get('title'), title_key,
                                 item['url'], item.get('cover'), item.get('preview_image'), item.get('duration'),
                                 now, now, now, now))
                            collected_ids.append(task_id); created += 1
                    db.execute("""UPDATE collection_tasks SET status='idle',last_total=?,last_created=?,
                        last_skipped=?,last_task_ids=?,last_error=NULL,last_run_at=?,updated=? WHERE id=?""",
                        (len(result['items']), created, updated, json.dumps(collected_ids), now, now, row['id']))
                self.scheduler_wake.set()
            except Exception as exc:
                message = str(exc) if isinstance(exc, CollectionError) else getattr(exc, 'message', None) or type(exc).__name__
                with self.db() as db:
                    db.execute("UPDATE collection_tasks SET status='idle',last_error=?,last_run_at=?,updated=? WHERE id=?",
                               (message[:300], time.time(), time.time(), row['id']))

    def run_scheduler(self):
        while not self.stop_event.is_set():
            try:
                self.queue_due_collections()
                settings = self.settings()
                self.downloads.set_scheduler(settings['enabled'], settings['concurrency'])
            except Exception:
                pass
            self.scheduler_wake.wait(1); self.scheduler_wake.clear()
