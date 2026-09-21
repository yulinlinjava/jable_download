"""Store browser-side JSON import formatter scripts in the shared SQLite database."""
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class ImportScriptError(Exception):
    pass


DEFAULT_SCRIPT = '''async function format(raw, helpers) {
  const items = Array.isArray(raw) ? raw : (raw.data?.list || raw.items || []);

  return items.map((item) => ({
    source: item.source || 'external',
    id: item.id || item.code,
    title: String(item.title || item.name || '').trim(),
    url: item.url || item.detailUrl || null,
    m3u8_url: item.m3u8_url || item.video_url || item.videoUrl || item.playUrl,
    cover: item.cover || item.coverUrl || null,
    poster: item.poster || item.posterUrl || null,
    preview_image: item.preview_image || item.previewUrl || null,
    vtt_url: item.vtt_url || item.vttUrl || null,
    duration: item.duration || null,
    published_at: item.published_at || item.publishedAt || null,
    actors: Array.isArray(item.actors) ? item.actors : [],
    tags: Array.isArray(item.tags) ? item.tags : []
  }));
}'''


class ImportScriptManager:
    def __init__(self, database):
        self.database = Path(database)
        with self.db() as db:
            existed = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='import_scripts'").fetchone()
            db.execute('''CREATE TABLE IF NOT EXISTS import_scripts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                description TEXT NOT NULL DEFAULT '', code TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL)''')
            if not existed:
                now = time.time()
                db.execute('''INSERT INTO import_scripts(id,name,description,code,created,updated)
                              VALUES(?,?,?,?,?,?)''',
                           (uuid.uuid4().hex, '通用字段映射',
                            '兼容数组、data.list 和 items，并映射常见字段名称。',
                            DEFAULT_SCRIPT, now, now))

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
        return dict(id=row['id'], name=row['name'], description=row['description'],
                    code=row['code'], created_at=row['created'], updated_at=row['updated'])

    @staticmethod
    def validate(name, description, code):
        name = (name or '').strip()
        description = (description or '').strip()
        code = (code or '').strip()
        if not name:
            raise ImportScriptError('请输入脚本名称')
        if len(name) > 100:
            raise ImportScriptError('脚本名称不能超过 100 个字符')
        if len(description) > 500:
            raise ImportScriptError('脚本说明不能超过 500 个字符')
        if not code:
            raise ImportScriptError('请输入格式化脚本')
        if len(code) > 100000:
            raise ImportScriptError('格式化脚本不能超过 100000 个字符')
        return name, description, code

    def list(self):
        with self.db() as db:
            rows = db.execute('SELECT * FROM import_scripts ORDER BY updated DESC').fetchall()
        return [self.serialize(row) for row in rows]

    def get(self, script_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM import_scripts WHERE id=?', (script_id,)).fetchone()
        return self.serialize(row) if row else None

    def create(self, name, description, code):
        name, description, code = self.validate(name, description, code)
        script_id, now = uuid.uuid4().hex, time.time()
        try:
            with self.db() as db:
                db.execute('''INSERT INTO import_scripts(id,name,description,code,created,updated)
                              VALUES(?,?,?,?,?,?)''',
                           (script_id, name, description, code, now, now))
        except sqlite3.IntegrityError:
            raise ImportScriptError('脚本名称已存在') from None
        return self.get(script_id)

    def update(self, script_id, name, description, code):
        name, description, code = self.validate(name, description, code)
        try:
            with self.db() as db:
                updated = db.execute('''UPDATE import_scripts SET name=?,description=?,code=?,updated=?
                                        WHERE id=?''',
                                     (name, description, code, time.time(), script_id)).rowcount
        except sqlite3.IntegrityError:
            raise ImportScriptError('脚本名称已存在') from None
        return self.get(script_id) if updated else None

    def delete(self, script_id):
        with self.db() as db:
            return bool(db.execute('DELETE FROM import_scripts WHERE id=?', (script_id,)).rowcount)
