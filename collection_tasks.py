"""Persistent list collection and download scheduling on the unified tasks table."""
import json
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


class CollectionError(Exception):
    pass


class CollectionManager:
    def __init__(self, root, downloads, collect, auto_start=True):
        self.database = downloads.database
        self.downloads = downloads
        self.collect = collect
        self.node_enqueue_callback = None
        self.stop_event, self.wake, self.scheduler_wake = threading.Event(), threading.Event(), threading.Event()
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS collection_tasks (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
                  batch_size INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'idle',
                  scheduled_enabled INTEGER NOT NULL DEFAULT 0, last_scheduled_date TEXT,
                  r2_sync_enabled INTEGER NOT NULL DEFAULT 0,
                  node_ids TEXT NOT NULL DEFAULT '[]',
                  last_total INTEGER NOT NULL DEFAULT 0, last_created INTEGER NOT NULL DEFAULT 0,
                  last_skipped INTEGER NOT NULL DEFAULT 0, last_task_ids TEXT NOT NULL DEFAULT '[]',
                  last_node_queued INTEGER NOT NULL DEFAULT 0,
                  last_node_skipped INTEGER NOT NULL DEFAULT 0,
                  last_node_failed INTEGER NOT NULL DEFAULT 0,
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
            ''')
            columns = {row['name'] for row in db.execute(
                'PRAGMA table_info(collection_tasks)').fetchall()}
            if 'r2_sync_enabled' not in columns:
                db.execute('''ALTER TABLE collection_tasks ADD COLUMN
                              r2_sync_enabled INTEGER NOT NULL DEFAULT 0''')
            for name, definition in (
                    ('node_ids', "TEXT NOT NULL DEFAULT '[]'"),
                    ('last_node_queued', 'INTEGER NOT NULL DEFAULT 0'),
                    ('last_node_skipped', 'INTEGER NOT NULL DEFAULT 0'),
                    ('last_node_failed', 'INTEGER NOT NULL DEFAULT 0')):
                if name not in columns:
                    db.execute(f'ALTER TABLE collection_tasks ADD COLUMN {name} {definition}')
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

    @staticmethod
    def serialize(row):
        if not row:
            return None
        value = dict(row)
        if 'last_task_ids' in value:
            value['last_task_ids'] = json.loads(value['last_task_ids'] or '[]')
        if 'node_ids' in value:
            try:
                value['node_ids'] = json.loads(value['node_ids'] or '[]')
            except (TypeError, ValueError):
                value['node_ids'] = []
        if 'created' in value:
            value['created_at'] = value.pop('created')
        if 'updated' in value:
            value['updated_at'] = value.pop('updated')
        if 'scheduled_enabled' in value:
            value['scheduled_enabled'] = bool(value['scheduled_enabled'])
        if 'r2_sync_enabled' in value:
            value['r2_sync_enabled'] = bool(value['r2_sync_enabled'])
        return value

    @staticmethod
    def normalized_title(title):
        if not title:
            return None
        value = ' '.join(unicodedata.normalize('NFKC', title).split()).casefold()
        return value or None

    def create(self, name, url, batch_size=1, scheduled_enabled=False,
               r2_sync_enabled=False, node_ids=None):
        now, task_id = time.time(), uuid.uuid4().hex
        node_ids = list(dict.fromkeys(node_ids or []))
        with self.db() as db:
            db.execute('''INSERT INTO collection_tasks(
                          id,name,url,batch_size,scheduled_enabled,r2_sync_enabled,node_ids,created,updated)
                          VALUES(?,?,?,?,?,?,?,?,?)''',
                       (task_id, name, url, 1, 1 if scheduled_enabled else 0,
                        1 if r2_sync_enabled else 0, json.dumps(node_ids), now, now))
        return self.get(task_id)

    def create_many(self, tasks, scheduled_enabled=False, r2_sync_enabled=False, node_ids=None):
        """Create collection tasks in one transaction and skip duplicate names or URLs."""
        created, skipped, results = [], [], []
        now = time.time()
        node_ids = list(dict.fromkeys(node_ids or []))
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
                    id,name,url,batch_size,scheduled_enabled,r2_sync_enabled,node_ids,created,updated)
                    VALUES(?,?,?,?,?,?,?,?,?)''',
                           (task_id, name, url, 1, 1 if scheduled_enabled else 0,
                            1 if r2_sync_enabled else 0, json.dumps(node_ids), now, now))
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
            conditions.append("lower(url) LIKE '%jable%'")
        elif source == '123av':
            conditions.append("lower(url) LIKE '%123av%'")
        elif source == 'other':
            conditions.append("lower(url) NOT LIKE '%jable%' AND lower(url) NOT LIKE '%123av%'")
        clause = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
        with self.db() as db:
            total = db.execute('SELECT count(*) FROM collection_tasks' + clause, values).fetchone()[0]
            rows = db.execute(
                'SELECT * FROM collection_tasks' + clause
                + ' ORDER BY updated DESC,id DESC LIMIT ? OFFSET ?',
                (*values, page_size, (page - 1) * page_size)).fetchall()
            running = db.execute(
                "SELECT count(*) FROM collection_tasks WHERE status IN ('queued','running')").fetchone()[0]
        return {
            'items': [self.serialize(row) for row in rows],
            'total': total,
            'page': page,
            'page_size': page_size,
            'running': running,
        }

    def update(self, task_id, name, url, batch_size=1, scheduled_enabled=False,
               r2_sync_enabled=False, node_ids=None):
        node_ids = list(dict.fromkeys(node_ids or []))
        with self.db() as db:
            row = db.execute('SELECT status FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                return None
            if row['status'] in ('queued', 'running'):
                raise CollectionError('任务执行中，暂时不能修改')
            db.execute('''UPDATE collection_tasks SET name=?,url=?,batch_size=1,
                          scheduled_enabled=?,r2_sync_enabled=?,node_ids=?,updated=? WHERE id=?''',
                       (name, url, 1 if scheduled_enabled else 0,
                        1 if r2_sync_enabled else 0, json.dumps(node_ids), time.time(), task_id))
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
                elif row['status'] in ('queued', 'running'):
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
                    segment_concurrency=row['segment_concurrency'],
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
                title=row['title'], url=row['url'], cover=task['cover_url'],
                preview_image=task['preview_url'], preview_m3u8=task['preview_m3u8_url'],
                preview5_m3u8=task['preview5_m3u8_url'],
                duration=row['duration'], status=row['status'],
                r2_synced=bool(row['r2_synced']),
                r2_sync_status=row['r2_sync_status'] or ('synced' if row['r2_synced'] else 'unsynced'),
                r2_synced_at=row['r2_synced_at'], local_deleted=bool(row['local_deleted']),
                local_deleted_at=row['local_deleted_at'],
                download_task_id=None if pending else row['id'], first_seen=row['first_seen'],
                last_seen=row['last_seen'], download=None if pending else task))
        return dict(page=page, page_size=page_size, total=total, items=items)

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
        local_linked = queued = updated = skipped = failed = 0
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
                    else:
                        queued += 1
                        message = '校验通过，将覆盖旧任务并重新排队'
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
                else:
                    queued += 1
                    message = '校验通过，远程资源将加入下载队列'
                results.append(dict(index=index, status='valid', message=message))
        return dict(total=len(items), valid=local_linked + queued, updated=updated,
                    local_linked=local_linked, queued=queued, skipped=skipped,
                    failed=failed, results=results)

    def import_items(self, items):
        from resource_import import validate_resource
        results, created_ids, updated_ids, queued_ids = [], [], [], []
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
                    status, message = ('completed', '本地资源已关联') if local else ('queued', '等待下载')
                    if local:
                        metadata['m3u8_url'] = local['url']
                        metadata['_local_resource'] = True
                        local_linked += 1
                    else:
                        queued_ids.append(existing['id'])
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
                                        message='旧资源已覆盖' + ('并重新加入下载队列' if not local else '')))
                    continue
                if existing:
                    results.append(dict(index=index, status='skipped', task_id=existing['id'],
                                        message=f'任务状态为 {existing["status"]}，已跳过覆盖'))
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
                    updated=len(updated_ids),
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
        if self.thread.is_alive():
            self.thread.join(timeout=35)
        if self.scheduler_thread.is_alive():
            self.scheduler_thread.join(timeout=5)

    def _save_collected_item(self, collection_id, item, now):
        title_key = self.normalized_title(item.get('title'))
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
                        cover_url=CASE WHEN cover_url LIKE '/api/download/%' THEN cover_url ELSE coalesce(?,cover_url) END,
                        preview_url=CASE WHEN preview_url LIKE '/api/download/%' THEN preview_url ELSE coalesce(?,preview_url) END,
                        duration=coalesce(?,duration),last_seen=?,updated=? WHERE id=?''',
                        (collection_id, item['source'], item.get('title'), title_key, item['url'],
                         item.get('cover'), item.get('preview_image'), item.get('duration'),
                         now, now, existing['id']))
                else:
                    db.execute('''UPDATE tasks SET collection_id=?,last_seen=?,updated=?,
                        cover_url=CASE WHEN cover_url LIKE '/api/download/%' THEN cover_url ELSE coalesce(cover_url,?) END,
                        preview_url=CASE WHEN preview_url LIKE '/api/download/%' THEN preview_url ELSE coalesce(preview_url,?) END,
                        duration=coalesce(duration,?) WHERE id=?''',
                        (collection_id, now, now, item.get('cover'), item.get('preview_image'),
                         item.get('duration'), existing['id']))
                return existing['id'], False
            task_id = uuid.uuid4().hex
            db.execute('''INSERT INTO tasks(id,collection_id,source,video_id,title,title_key,url,
                cover_url,preview_url,duration,status,message,first_seen,last_seen,created,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?,'pending','等待加入下载',?,?,?,?)''',
                (task_id, collection_id, item['source'], item['id'], item.get('title'), title_key,
                 item['url'], item.get('cover'), item.get('preview_image'), item.get('duration'),
                 now, now, now, now))
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
            collected_ids = []
            try:
                result = self.collect(row['url'])
                now = time.time()
                try:
                    node_ids = json.loads(row['node_ids'] or '[]')
                except (TypeError, ValueError):
                    node_ids = []
                for item in result['items']:
                    task_id, was_created = self._save_collected_item(row['id'], item, now)
                    collected_ids.append(task_id)
                    if was_created:
                        created += 1
                    else:
                        updated += 1
                    if node_ids and self.node_enqueue_callback:
                        try:
                            queued = self.node_enqueue_callback(node_ids, [task_id])
                            node_queued += queued.get('queued', 0)
                            node_skipped += queued.get('skipped', 0)
                            node_failed += queued.get('failed', 0)
                        except Exception:
                            node_failed += len(node_ids)
                with self.db() as db:
                    db.execute("""UPDATE collection_tasks SET status='idle',last_total=?,last_created=?,
                        last_skipped=?,last_task_ids=?,last_node_queued=?,last_node_skipped=?,
                        last_node_failed=?,last_error=NULL,last_run_at=?,updated=? WHERE id=?""",
                        (len(result['items']), created, updated, json.dumps(collected_ids),
                         node_queued, node_skipped, node_failed, now, now, row['id']))
                self.scheduler_wake.set()
            except Exception as exc:
                message = str(exc) if isinstance(exc, CollectionError) else getattr(exc, 'message', None) or type(exc).__name__
                with self.db() as db:
                    failed_at = time.time()
                    db.execute('''UPDATE collection_tasks SET status='idle',last_total=?,last_created=?,
                        last_skipped=?,last_task_ids=?,last_node_queued=?,last_node_skipped=?,
                        last_node_failed=?,last_error=?,last_run_at=?,updated=? WHERE id=?''',
                        (len(collected_ids), created, updated, json.dumps(collected_ids),
                         node_queued, node_skipped, node_failed, message[:300],
                         failed_at, failed_at, row['id']))

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
