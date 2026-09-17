"""Persistent list collection and download scheduling on the unified tasks table."""
import json
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime
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
                preview_image=row['preview_url'], duration=row['duration'], status=row['status'],
                download_task_id=None if pending else row['id'], first_seen=row['first_seen'],
                last_seen=row['last_seen'], download=None if pending else task))
        return dict(page=page, page_size=page_size, total=total, items=items)

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
                                    cover_url=coalesce(?,cover_url),preview_url=coalesce(?,preview_url),
                                    duration=coalesce(?,duration),last_seen=?,updated=? WHERE id=?''',
                                    (row['id'], item['source'], item.get('title'), title_key, item['url'],
                                     item.get('cover'), item.get('preview_image'), item.get('duration'),
                                     now, now, existing['id']))
                            else:
                                db.execute('''UPDATE tasks SET collection_id=?,last_seen=?,updated=?,
                                    cover_url=coalesce(cover_url,?),preview_url=coalesce(preview_url,?),
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
