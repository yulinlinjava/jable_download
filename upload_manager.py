"""Persistent, resumable and atomic chunked file uploads."""
import base64
import json
import hashlib
import logging
import math
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import quote, unquote, urlsplit

from media_previews import MediaPreviewGenerator


log = logging.getLogger(__name__)


class UploadError(Exception):
    def __init__(self, status, code, message, data=None):
        self.status = status
        self.code = code
        self.message = message
        self.data = data


def positive_env(name, default):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f'{name} must be an integer') from exc
    if value <= 0:
        raise ValueError(f'{name} must be positive')
    return value


def boolean_env(name, default):
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.strip().lower() not in {'0', 'false', 'no', 'off'}


def bounded_env(name, default, minimum, maximum):
    value = positive_env(name, default)
    if value < minimum or value > maximum:
        raise ValueError(f'{name} must be between {minimum} and {maximum}')
    return value


class UploadManager:
    BUFFER_SIZE = 1024 * 1024
    BACKGROUND_RETRY_MAX_SECONDS = 60
    BACKGROUND_ERROR_LOG_INTERVAL = 60
    BLOCKED_EXTENSIONS = {
        # Server-side scripts and source code.
        '.asp', '.aspx', '.ashx', '.asmx', '.c', '.cc', '.cgi', '.class',
        '.cpp', '.cs', '.go', '.h', '.hpp',
        '.groovy', '.java', '.jar', '.jsp', '.jspx', '.kt', '.lua', '.php',
        '.php3', '.php4', '.php5', '.phtml', '.pl', '.py', '.pyc', '.rb',
        '.rs', '.scala', '.sh', '.swift', '.war',
        # Browser-executable content. Uploads are served from the same origin.
        '.css', '.htm', '.html', '.js', '.jsx', '.mjs', '.svg', '.tsx',
        '.vue', '.wasm', '.xml', '.xhtml',
        # Executables, installers, command files and shortcuts.
        '.app', '.apk', '.bat', '.bin', '.cmd', '.com', '.cpl', '.deb', '.dll',
        '.dmg', '.exe', '.hta', '.iso', '.lnk', '.msi', '.ps1', '.reg', '.rpm',
        '.scr', '.sys', '.vbe', '.vbs', '.wsf',
        # Server/config files that should never be published as uploads.
        '.conf', '.config', '.env', '.htaccess', '.ini', '.pem', '.sql', '.yaml',
        '.yml',
    }

    def __init__(self, download_root, database=None, public_base_url_getter=None,
                 completion_callback=None):
        self._config_lock = threading.RLock()
        self.root = (Path(download_root).resolve() / 'uploads').resolve()
        self.chunks_root = self.root / 'chunks'
        # Public uploads are separated by type. The chunks directory remains private.
        self.files_root = self.root
        self.category_roots = {
            category: self.root / category for category in ('video', 'image', 'other')
        }
        self.chunks_root.mkdir(parents=True, exist_ok=True)
        for category_root in self.category_roots.values():
            category_root.mkdir(parents=True, exist_ok=True)
        self.database = Path(database).resolve() if database else Path(download_root).resolve() / 'tasks.sqlite3'
        self.completion_callback = completion_callback
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=NORMAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS uploads (
                  identifier TEXT PRIMARY KEY, file_name TEXT, file_type TEXT,
                  media_type TEXT, size INTEGER NOT NULL DEFAULT 0,
                  total_chunks INTEGER, uploaded_count INTEGER NOT NULL DEFAULT 0,
                  completed INTEGER NOT NULL DEFAULT 0,
                  original_deleted INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL DEFAULT 'uploading', processing_stage TEXT,
                  message TEXT, error TEXT, warnings TEXT NOT NULL DEFAULT '[]',
                  path TEXT, hls_path TEXT, cover_path TEXT, poster_path TEXT,
                  preview_path TEXT, vtt_path TEXT, preview_m3u8_path TEXT,
                  preview5_m3u8_path TEXT, source_file TEXT,
                  duration REAL, width INTEGER, height INTEGER,
                  is_portrait INTEGER NOT NULL DEFAULT 0, image_format TEXT,
                  video_codec TEXT, audio_codec TEXT, bit_rate INTEGER,
                  original_size INTEGER, processed_size INTEGER,
                  compressed INTEGER NOT NULL DEFAULT 0, compression_ratio REAL,
                  r2_sync_enabled INTEGER NOT NULL DEFAULT 0,
                  r2_synced INTEGER NOT NULL DEFAULT 0,
                  r2_sync_status TEXT NOT NULL DEFAULT 'unsynced',
                  r2_synced_at REAL,
                  local_deleted INTEGER NOT NULL DEFAULT 0,
                  local_deleted_at REAL,
                  created REAL NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS uploads_updated
                  ON uploads(updated DESC,identifier);
                CREATE INDEX IF NOT EXISTS uploads_type_status_updated
                  ON uploads(file_type,status,updated DESC);
                CREATE INDEX IF NOT EXISTS uploads_status_updated
                  ON uploads(status,updated DESC,identifier);
                CREATE INDEX IF NOT EXISTS uploads_name
                  ON uploads(file_name COLLATE NOCASE);
                CREATE TABLE IF NOT EXISTS ffmpeg_tasks (
                  id TEXT PRIMARY KEY, upload_identifier TEXT NOT NULL UNIQUE,
                  file_name TEXT, status TEXT NOT NULL DEFAULT 'queued',
                  stage TEXT NOT NULL DEFAULT 'queued', progress REAL NOT NULL DEFAULT 0,
                  compression_required INTEGER, compression_reason TEXT,
                  message TEXT, error TEXT, created REAL NOT NULL,
                  queued_at REAL NOT NULL, started_at REAL, completed_at REAL,
                  updated REAL NOT NULL,
                  FOREIGN KEY(upload_identifier) REFERENCES uploads(identifier));
                CREATE INDEX IF NOT EXISTS ffmpeg_tasks_status_created
                  ON ffmpeg_tasks(status,created,id);
                CREATE INDEX IF NOT EXISTS ffmpeg_tasks_updated
                  ON ffmpeg_tasks(updated DESC,id);
                CREATE TABLE IF NOT EXISTS ffmpeg_settings (
                  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS upload_chunk_cleanup_queue (
                  identifier TEXT PRIMARY KEY,
                  expires_at REAL NOT NULL,
                  chunk_bytes INTEGER NOT NULL DEFAULT 0,
                  uploaded_count INTEGER NOT NULL DEFAULT 0,
                  updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS upload_chunk_cleanup_expiry
                  ON upload_chunk_cleanup_queue(expires_at);
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(uploads)')}
            migrations = {
                'preview_m3u8_path': 'TEXT', 'preview5_m3u8_path': 'TEXT',
                'duration': 'REAL', 'width': 'INTEGER', 'height': 'INTEGER',
                'is_portrait': 'INTEGER NOT NULL DEFAULT 0', 'image_format': 'TEXT',
                'video_codec': 'TEXT', 'audio_codec': 'TEXT', 'bit_rate': 'INTEGER',
                'original_size': 'INTEGER', 'processed_size': 'INTEGER',
                'compressed': 'INTEGER NOT NULL DEFAULT 0', 'compression_ratio': 'REAL',
                'r2_sync_enabled': 'INTEGER NOT NULL DEFAULT 0',
                'r2_synced': 'INTEGER NOT NULL DEFAULT 0',
                'r2_sync_status': "TEXT NOT NULL DEFAULT 'unsynced'",
                'r2_synced_at': 'REAL',
                'local_deleted': 'INTEGER NOT NULL DEFAULT 0',
                'local_deleted_at': 'REAL',
            }
            for name, definition in migrations.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE uploads ADD COLUMN {name} {definition}')
            db.execute("""UPDATE uploads SET r2_sync_status=CASE
                          WHEN r2_synced=1 THEN 'synced' ELSE 'unsynced' END
                          WHERE r2_sync_status IS NULL OR r2_sync_status NOT IN
                          ('unsynced','syncing','synced')""")
            db.execute("UPDATE uploads SET r2_sync_status='synced' WHERE r2_synced=1")
            if 'r2_sync_tasks' in {row['name'] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}:
                db.execute("""UPDATE uploads SET r2_sync_status='syncing' WHERE EXISTS(
                              SELECT 1 FROM r2_sync_tasks r WHERE r.resource_type='upload'
                              AND r.resource_id=uploads.identifier
                              AND r.status IN ('queued','running'))""")
                db.execute("""UPDATE uploads SET r2_synced_at=coalesce(r2_synced_at,(
                              SELECT r.completed FROM r2_sync_tasks r
                              WHERE r.resource_type='upload' AND r.resource_id=uploads.identifier
                              AND r.status='completed' ORDER BY r.completed DESC LIMIT 1))
                              WHERE r2_synced=1""")
            db.execute('DROP INDEX IF EXISTS uploads_chunk_expiry')
            db.execute('''DELETE FROM upload_chunk_cleanup_queue
                          WHERE NOT EXISTS(SELECT 1 FROM uploads
                            WHERE uploads.identifier=upload_chunk_cleanup_queue.identifier
                              AND uploads.status='uploading')''')
        self._public_base_url_getter = public_base_url_getter
        self.public_base_url = os.getenv('PUBLIC_BASE_URL', 'http://localhost:8000').rstrip('/')
        self.max_chunk_bytes = positive_env('UPLOAD_CHUNK_MAX_BYTES', 50 * 1024 * 1024)
        self.max_file_bytes = positive_env('UPLOAD_FILE_MAX_BYTES', 20 * 1024 * 1024 * 1024)
        self.max_chunks = positive_env('UPLOAD_MAX_CHUNKS', 100000)
        self.hls_time = positive_env('UPLOAD_HLS_TIME', 3)
        self.preview_interval = positive_env('UPLOAD_PREVIEW_INTERVAL', 10)
        self.video_timeout = positive_env('UPLOAD_VIDEO_TIMEOUT', 3600)
        self.compress_enabled = boolean_env('UPLOAD_COMPRESS_ENABLED', True)
        self.compress_compatibility_only = boolean_env(
            'UPLOAD_COMPATIBILITY_ONLY_COMPRESSION', True)
        self.generate_hover_preview = boolean_env('UPLOAD_GENERATE_HOVER_PREVIEW', True)
        self.generate_vtt_preview = boolean_env('UPLOAD_GENERATE_VTT_PREVIEW', True)
        self.compress_min_bytes = positive_env('UPLOAD_COMPRESS_MIN_BYTES', 500 * 1024 * 1024)
        self.compress_min_duration = positive_env('UPLOAD_COMPRESS_MIN_DURATION', 30 * 60)
        self.compress_max_width = positive_env('UPLOAD_COMPRESS_MAX_WIDTH', 1920)
        self.compress_max_height = positive_env('UPLOAD_COMPRESS_MAX_HEIGHT', 1920)
        self.compress_max_video_bitrate = positive_env(
            'UPLOAD_COMPRESS_MAX_VIDEO_BITRATE', 5_000_000)
        self.compress_crf = bounded_env('UPLOAD_COMPRESS_CRF', 25, 0, 51)
        self.compress_preset = os.getenv('UPLOAD_COMPRESS_PRESET', 'superfast').strip().lower()
        if self.compress_preset not in {
                'ultrafast', 'superfast', 'veryfast', 'faster', 'fast',
                'medium', 'slow', 'slower', 'veryslow'}:
            raise ValueError('UPLOAD_COMPRESS_PRESET is invalid')
        self.compress_audio_bitrate = os.getenv('UPLOAD_AUDIO_BITRATE', '128k').strip().lower()
        if not re.fullmatch(r'[1-9][0-9]{1,4}k', self.compress_audio_bitrate):
            raise ValueError('UPLOAD_AUDIO_BITRATE must look like 128k')
        self._compression_defaults = self._compression_snapshot()
        with self.db() as db:
            now = time.time()
            for key, value in self._compression_storage(self._compression_defaults).items():
                db.execute('''INSERT OR IGNORE INTO ffmpeg_settings(key,value,updated)
                              VALUES(?,?,?)''', (key, str(value), now))
        self._reload_compression_settings()
        self.image_compress_enabled = boolean_env('UPLOAD_IMAGE_COMPRESS_ENABLED', True)
        self.image_compress_min_bytes = positive_env(
            'UPLOAD_IMAGE_COMPRESS_MIN_BYTES', 5 * 1024 * 1024)
        self.image_compress_quality = bounded_env(
            'UPLOAD_IMAGE_COMPRESS_QUALITY', 82, 1, 100)
        self._image_compression_defaults = self._image_compression_snapshot()
        with self.db() as db:
            now = time.time()
            for key, value in self._image_compression_storage(
                    self._image_compression_defaults).items():
                db.execute('''INSERT OR IGNORE INTO ffmpeg_settings(key,value,updated)
                              VALUES(?,?,?)''', (key, str(value), now))
        self._reload_image_compression_settings()
        self.chunk_cleanup_enabled = boolean_env('UPLOAD_CHUNK_CLEANUP_ENABLED', True)
        self.chunk_retention_days = bounded_env(
            'UPLOAD_CHUNK_RETENTION_DAYS', 7, 1, 3650)
        self._chunk_cleanup_defaults = {
            'enabled': self.chunk_cleanup_enabled,
            'retentionDays': self.chunk_retention_days,
        }
        with self.db() as db:
            now = time.time()
            for key, value in (
                    ('chunk_cleanup.enabled', int(self.chunk_cleanup_enabled)),
                    ('chunk_cleanup.retention_days', self.chunk_retention_days)):
                db.execute('''INSERT OR IGNORE INTO ffmpeg_settings(key,value,updated)
                              VALUES(?,?,?)''', (key, str(value), now))
        self._reload_chunk_cleanup_settings()
        self.preview_generator = MediaPreviewGenerator(self.video_timeout, self.preview_interval)
        self.merge_workers = positive_env('UPLOAD_MERGE_WORKERS', 1)
        self.ffmpeg_max_concurrency = positive_env('UPLOAD_FFMPEG_MAX_CONCURRENCY', 16)
        default_ffmpeg_concurrency = int(os.getenv('UPLOAD_FFMPEG_CONCURRENCY', '1'))
        if default_ffmpeg_concurrency < 0 or default_ffmpeg_concurrency > self.ffmpeg_max_concurrency:
            raise ValueError('UPLOAD_FFMPEG_CONCURRENCY is outside the allowed range')
        with self.db() as db:
            db.execute('''INSERT OR IGNORE INTO ffmpeg_settings(key,value,updated)
                          VALUES('concurrency',?,?)''',
                       (str(default_ffmpeg_concurrency), time.time()))
            db.execute("""UPDATE ffmpeg_tasks SET status='queued',stage='queued',
                          message='服务重启，任务已重新排队',started_at=NULL,updated=?
                          WHERE status IN ('probing','compressing','segmenting',
                          'generating_cover','generating_preview','generating_vtt','running')""",
                       (time.time(),))
            db.execute("""UPDATE uploads SET status='queued',processing_stage='queued',
                          message='服务重启，FFmpeg 任务已重新排队',updated=?
                          WHERE identifier IN (SELECT upload_identifier FROM ffmpeg_tasks
                          WHERE status='queued')""", (time.time(),))
        self._guard = threading.Lock()
        self._locks = {}
        self._merge_futures = {}
        self._ffmpeg_futures = {}
        self._runtime_lock = threading.RLock()
        self._ffmpeg_runtime = {}
        self._ffmpeg_wakeup = threading.Event()
        self._chunk_cleanup_wakeup = threading.Event()
        self._closed = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=self.merge_workers,
                                            thread_name_prefix='upload-merge')
        self._ffmpeg_executor = ThreadPoolExecutor(
            max_workers=self.ffmpeg_max_concurrency, thread_name_prefix='upload-ffmpeg')
        self._ffmpeg_dispatcher = threading.Thread(
            target=self._ffmpeg_dispatch_loop, name='upload-ffmpeg-dispatch', daemon=True)
        self._chunk_cleanup_thread = threading.Thread(
            target=self._chunk_cleanup_loop, name='upload-chunk-cleanup', daemon=True)
        # A process restart means no upload is still writing these private files.
        for temporary in self.files_root.glob('.*.part'):
            temporary.unlink(missing_ok=True)
        for temporary in self.chunks_root.glob('*/*/*/.upload-*'):
            temporary.unlink(missing_ok=True)
        with self.db() as db:
            resumable = db.execute("""SELECT * FROM uploads
                WHERE total_chunks>0 AND status='merging'""").fetchall()
        for row in resumable:
            metadata = self._metadata_from_row(row)
            if (metadata.get('fileType') in self.category_roots
                    and metadata.get('status') == 'merging'):
                try:
                    self.merge(metadata.get('identifier'), metadata.get('fileName'))
                except Exception:
                    log.exception('Unable to resume upload merge %s', metadata.get('identifier'))
        self._ffmpeg_dispatcher.start()
        self._ffmpeg_wakeup.set()
        self._chunk_cleanup_thread.start()

    def close(self):
        self._closed.set()
        self._ffmpeg_wakeup.set()
        self._chunk_cleanup_wakeup.set()
        if self._ffmpeg_dispatcher.is_alive():
            self._ffmpeg_dispatcher.join(timeout=2)
        if self._chunk_cleanup_thread.is_alive():
            self._chunk_cleanup_thread.join(timeout=2)
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._ffmpeg_executor.shutdown(wait=False, cancel_futures=True)

    def _notify_completed(self, metadata):
        if not self.completion_callback or not metadata or not metadata.get('completed'):
            return
        try:
            self.completion_callback(dict(metadata))
        except Exception:
            # R2 is an optional downstream copy. It must never turn a completed
            # local upload into a failed upload response.
            log.exception('Unable to enqueue upload %s for downstream synchronization',
                          metadata.get('identifier'))

    def _runtime_started(self, identifier, process, stage):
        if not identifier:
            return
        with self._runtime_lock:
            self._ffmpeg_runtime[identifier] = {
                'taskId': identifier, 'pid': process.pid, 'stage': stage,
                'startedAt': time.time(), 'frame': None, 'speed': None,
                'outTime': None,
            }

    def _runtime_updated(self, identifier, **values):
        if not identifier:
            return
        with self._runtime_lock:
            current = self._ffmpeg_runtime.get(identifier)
            if current:
                current.update({key: value for key, value in values.items()
                                if value not in (None, '')})

    def _runtime_finished(self, identifier, pid):
        if not identifier:
            return
        with self._runtime_lock:
            current = self._ffmpeg_runtime.get(identifier)
            if current and current.get('pid') == pid:
                self._ffmpeg_runtime.pop(identifier, None)

    def ffmpeg_runtime_processes(self):
        with self._runtime_lock:
            return [dict(item) for item in self._ffmpeg_runtime.values()]

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _lock(self, identifier):
        with self._guard:
            return self._locks.setdefault(identifier, threading.RLock())

    @classmethod
    def _background_retry_delay(cls, failures, base=1):
        return min(
            cls.BACKGROUND_RETRY_MAX_SECONDS,
            max(1, base) * (2 ** min(max(0, failures - 1), 6)))

    def _compression_snapshot(self):
        with self._config_lock:
            return {
                'enabled': bool(self.compress_enabled),
                'compatibilityOnly': bool(self.compress_compatibility_only),
                'generateHoverPreview': bool(self.generate_hover_preview),
                'generateVttPreview': bool(self.generate_vtt_preview),
                'minBytes': int(self.compress_min_bytes),
                'minDuration': int(self.compress_min_duration),
                'maxWidth': int(self.compress_max_width),
                'maxHeight': int(self.compress_max_height),
                'maxVideoBitrate': int(self.compress_max_video_bitrate),
                'crf': int(self.compress_crf), 'preset': self.compress_preset,
                'audioBitrate': self.compress_audio_bitrate,
                'hlsTime': int(self.hls_time),
            }

    @staticmethod
    def _compression_storage(config):
        return {
            'compression.enabled': int(bool(config['enabled'])),
            'compression.compatibility_only': int(bool(config['compatibilityOnly'])),
            'preview.hover_enabled': int(bool(config['generateHoverPreview'])),
            'preview.vtt_enabled': int(bool(config['generateVttPreview'])),
            'compression.min_bytes': int(config['minBytes']),
            'compression.min_duration': int(config['minDuration']),
            'compression.max_width': int(config['maxWidth']),
            'compression.max_height': int(config['maxHeight']),
            'compression.max_video_bitrate': int(config['maxVideoBitrate']),
            'compression.crf': int(config['crf']),
            'compression.preset': config['preset'],
            'compression.audio_bitrate': config['audioBitrate'],
            'compression.hls_time': int(config['hlsTime']),
        }

    def _apply_compression_snapshot(self, config):
        with self._config_lock:
            self.compress_enabled = bool(config['enabled'])
            self.compress_compatibility_only = bool(config['compatibilityOnly'])
            self.generate_hover_preview = bool(config['generateHoverPreview'])
            self.generate_vtt_preview = bool(config['generateVttPreview'])
            self.compress_min_bytes = int(config['minBytes'])
            self.compress_min_duration = int(config['minDuration'])
            self.compress_max_width = int(config['maxWidth'])
            self.compress_max_height = int(config['maxHeight'])
            self.compress_max_video_bitrate = int(config['maxVideoBitrate'])
            self.compress_crf = int(config['crf'])
            self.compress_preset = str(config['preset'])
            self.compress_audio_bitrate = str(config['audioBitrate'])
            self.hls_time = int(config['hlsTime'])

    def _reload_compression_settings(self):
        defaults = dict(self._compression_defaults)
        keys = tuple(self._compression_storage(defaults))
        placeholders = ','.join('?' for _ in keys)
        with self.db() as db:
            rows = db.execute(
                f'SELECT key,value FROM ffmpeg_settings WHERE key IN ({placeholders})',
                keys).fetchall()
        stored = {row['key']: row['value'] for row in rows}
        try:
            config = {
                'enabled': stored.get('compression.enabled', '1') == '1',
                'compatibilityOnly': stored.get(
                    'compression.compatibility_only',
                    '1' if defaults['compatibilityOnly'] else '0') == '1',
                'generateHoverPreview': stored.get(
                    'preview.hover_enabled',
                    '1' if defaults['generateHoverPreview'] else '0') == '1',
                'generateVttPreview': stored.get(
                    'preview.vtt_enabled',
                    '1' if defaults['generateVttPreview'] else '0') == '1',
                'minBytes': int(stored.get('compression.min_bytes', defaults['minBytes'])),
                'minDuration': int(stored.get('compression.min_duration', defaults['minDuration'])),
                'maxWidth': int(stored.get('compression.max_width', defaults['maxWidth'])),
                'maxHeight': int(stored.get('compression.max_height', defaults['maxHeight'])),
                'maxVideoBitrate': int(stored.get(
                    'compression.max_video_bitrate', defaults['maxVideoBitrate'])),
                'crf': int(stored.get('compression.crf', defaults['crf'])),
                'preset': stored.get('compression.preset', defaults['preset']),
                'audioBitrate': stored.get(
                    'compression.audio_bitrate', defaults['audioBitrate']),
                'hlsTime': int(stored.get('compression.hls_time', defaults['hlsTime'])),
            }
            self._validate_compression_snapshot(config)
        except (TypeError, ValueError, UploadError):
            log.warning('Invalid stored FFmpeg compression settings; using environment defaults')
            config = defaults
        self._apply_compression_snapshot(config)

    @staticmethod
    def _validate_compression_snapshot(config):
        ranges = {
            'minBytes': (1024 * 1024, 100 * 1024 * 1024 * 1024),
            'minDuration': (60, 24 * 3600), 'maxWidth': (320, 7680),
            'maxHeight': (240, 4320), 'maxVideoBitrate': (100_000, 100_000_000),
            'crf': (0, 51), 'hlsTime': (1, 30),
        }
        for field, (minimum, maximum) in ranges.items():
            value = config.get(field)
            if not isinstance(value, int) or value < minimum or value > maximum:
                raise UploadError(400, 40023, f'压缩配置 {field} 超出允许范围')
        presets = {'ultrafast', 'superfast', 'veryfast', 'faster', 'fast',
                   'medium', 'slow', 'slower', 'veryslow'}
        if config.get('preset') not in presets:
            raise UploadError(400, 40023, 'FFmpeg 编码速度参数无效')
        if not re.fullmatch(r'[1-9][0-9]{1,4}k', str(config.get('audioBitrate') or '')):
            raise UploadError(400, 40023, '音频码率参数无效')
        return config

    def compression_settings(self):
        config = self._compression_snapshot()
        return {
            'enabled': config['enabled'],
            'compatibilityOnly': config['compatibilityOnly'],
            'generateHoverPreview': config['generateHoverPreview'],
            'generateVttPreview': config['generateVttPreview'],
            'minFileSizeMb': round(config['minBytes'] / 1024 / 1024, 2),
            'minDurationMinutes': round(config['minDuration'] / 60, 2),
            'maxVideoBitrateMbps': round(config['maxVideoBitrate'] / 1_000_000, 3),
            'maxWidth': config['maxWidth'], 'maxHeight': config['maxHeight'],
            'crf': config['crf'], 'preset': config['preset'],
            'audioBitrateKbps': int(config['audioBitrate'][:-1]),
            'hlsSegmentSeconds': config['hlsTime'],
            'presets': ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast',
                        'medium', 'slow', 'slower', 'veryslow'],
        }

    def set_compression_settings(self, values):
        try:
            config = {
                'enabled': bool(values['enabled']),
                'compatibilityOnly': bool(values['compatibilityOnly']),
                'generateHoverPreview': bool(values['generateHoverPreview']),
                'generateVttPreview': bool(values['generateVttPreview']),
                'minBytes': round(float(values['minFileSizeMb']) * 1024 * 1024),
                'minDuration': round(float(values['minDurationMinutes']) * 60),
                'maxVideoBitrate': round(float(values['maxVideoBitrateMbps']) * 1_000_000),
                'maxWidth': int(values['maxWidth']), 'maxHeight': int(values['maxHeight']),
                'crf': int(values['crf']), 'preset': str(values['preset']).strip().lower(),
                'audioBitrate': f"{int(values['audioBitrateKbps'])}k",
                'hlsTime': int(values['hlsSegmentSeconds']),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise UploadError(400, 40023, '压缩配置格式无效') from exc
        self._validate_compression_snapshot(config)
        now = time.time()
        with self._config_lock:
            with self.db() as db:
                for key, value in self._compression_storage(config).items():
                    db.execute('''INSERT INTO ffmpeg_settings(key,value,updated) VALUES(?,?,?)
                                  ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                  updated=excluded.updated''', (key, str(value), now))
            self._apply_compression_snapshot(config)
        return self.compression_settings()

    def reset_compression_settings(self):
        config = dict(self._compression_defaults)
        now = time.time()
        with self._config_lock:
            with self.db() as db:
                for key, value in self._compression_storage(config).items():
                    db.execute('''INSERT INTO ffmpeg_settings(key,value,updated) VALUES(?,?,?)
                                  ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                  updated=excluded.updated''', (key, str(value), now))
            self._apply_compression_snapshot(config)
        return self.compression_settings()

    def _image_compression_snapshot(self):
        with self._config_lock:
            return {
                'enabled': bool(self.image_compress_enabled),
                'minBytes': int(self.image_compress_min_bytes),
                'quality': int(self.image_compress_quality),
            }

    @staticmethod
    def _image_compression_storage(config):
        return {
            'image_compression.enabled': int(bool(config['enabled'])),
            'image_compression.min_bytes': int(config['minBytes']),
            'image_compression.quality': int(config['quality']),
        }

    @staticmethod
    def _validate_image_compression_snapshot(config):
        min_bytes = config.get('minBytes')
        quality = config.get('quality')
        if (not isinstance(min_bytes, int) or min_bytes < 100 * 1024
                or min_bytes > 100 * 1024 * 1024 * 1024):
            raise UploadError(400, 40024, '图片压缩大小阈值超出允许范围')
        if not isinstance(quality, int) or quality < 1 or quality > 100:
            raise UploadError(400, 40024, '图片压缩质量必须在 1 到 100 之间')
        return config

    def _apply_image_compression_snapshot(self, config):
        with self._config_lock:
            self.image_compress_enabled = bool(config['enabled'])
            self.image_compress_min_bytes = int(config['minBytes'])
            self.image_compress_quality = int(config['quality'])

    def _reload_image_compression_settings(self):
        defaults = dict(self._image_compression_defaults)
        keys = tuple(self._image_compression_storage(defaults))
        placeholders = ','.join('?' for _ in keys)
        with self.db() as db:
            rows = db.execute(
                f'SELECT key,value FROM ffmpeg_settings WHERE key IN ({placeholders})',
                keys).fetchall()
        stored = {row['key']: row['value'] for row in rows}
        try:
            config = {
                'enabled': stored.get('image_compression.enabled', '1') == '1',
                'minBytes': int(stored.get(
                    'image_compression.min_bytes', defaults['minBytes'])),
                'quality': int(stored.get(
                    'image_compression.quality', defaults['quality'])),
            }
            self._validate_image_compression_snapshot(config)
        except (TypeError, ValueError, UploadError):
            log.warning('Invalid stored image compression settings; using environment defaults')
            config = defaults
        self._apply_image_compression_snapshot(config)

    def image_compression_settings(self):
        config = self._image_compression_snapshot()
        return {
            'enabled': config['enabled'],
            'minFileSizeMb': round(config['minBytes'] / 1024 / 1024, 2),
            'quality': config['quality'],
        }

    def set_image_compression_settings(self, values):
        try:
            config = {
                'enabled': bool(values['enabled']),
                'minBytes': round(float(values['minFileSizeMb']) * 1024 * 1024),
                'quality': int(values['quality']),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise UploadError(400, 40024, '图片压缩配置格式无效') from exc
        self._validate_image_compression_snapshot(config)
        now = time.time()
        with self._config_lock:
            with self.db() as db:
                for key, value in self._image_compression_storage(config).items():
                    db.execute('''INSERT INTO ffmpeg_settings(key,value,updated) VALUES(?,?,?)
                                  ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                  updated=excluded.updated''', (key, str(value), now))
            self._apply_image_compression_snapshot(config)
        return self.image_compression_settings()

    def _reload_chunk_cleanup_settings(self):
        defaults = self._chunk_cleanup_defaults
        with self.db() as db:
            rows = db.execute('''SELECT key,value FROM ffmpeg_settings
                WHERE key IN ('chunk_cleanup.enabled','chunk_cleanup.retention_days')''').fetchall()
        stored = {row['key']: row['value'] for row in rows}
        try:
            enabled = stored.get('chunk_cleanup.enabled', '1') == '1'
            retention_days = int(stored.get(
                'chunk_cleanup.retention_days', defaults['retentionDays']))
            if retention_days < 1 or retention_days > 3650:
                raise ValueError()
        except (TypeError, ValueError):
            enabled = bool(defaults['enabled'])
            retention_days = int(defaults['retentionDays'])
        with self._config_lock:
            self.chunk_cleanup_enabled = enabled
            self.chunk_retention_days = retention_days

    def chunk_cleanup_settings(self):
        with self._config_lock:
            enabled = bool(self.chunk_cleanup_enabled)
            retention_days = int(self.chunk_retention_days)
        with self.db() as db:
            summary = db.execute('''SELECT count(*) total,
                coalesce(sum(chunk_bytes),0) bytes,
                coalesce(sum(CASE WHEN expires_at<=? THEN 1 ELSE 0 END),0) expired
                FROM upload_chunk_cleanup_queue''',
                                 (time.time(),)).fetchone()
            stored = {row['key']: row['value'] for row in db.execute('''SELECT key,value
                FROM ffmpeg_settings WHERE key IN ('chunk_cleanup.last_run',
                'chunk_cleanup.last_deleted','chunk_cleanup.last_bytes')''')}
        return {
            'enabled': enabled, 'retentionDays': retention_days,
            'incompleteCount': int(summary['total'] or 0),
            'incompleteBytes': int(summary['bytes'] or 0),
            'expiredCount': int(summary['expired'] or 0),
            'lastCleanupAt': float(stored.get('chunk_cleanup.last_run') or 0),
            'lastDeleted': int(stored.get('chunk_cleanup.last_deleted') or 0),
            'lastFreedBytes': int(stored.get('chunk_cleanup.last_bytes') or 0),
        }

    def set_chunk_cleanup_settings(self, values):
        try:
            enabled = bool(values['enabled'])
            retention_days = int(values['retentionDays'])
        except (KeyError, TypeError, ValueError) as exc:
            raise UploadError(400, 40025, '分片清理配置格式无效') from exc
        if retention_days < 1 or retention_days > 3650:
            raise UploadError(400, 40025, '分片保留天数必须在 1 到 3650 之间')
        now = time.time()
        with self._config_lock:
            with self.db() as db:
                for key, value in (
                        ('chunk_cleanup.enabled', int(enabled)),
                        ('chunk_cleanup.retention_days', retention_days)):
                    db.execute('''INSERT INTO ffmpeg_settings(key,value,updated) VALUES(?,?,?)
                                  ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                  updated=excluded.updated''', (key, str(value), now))
                db.execute('''UPDATE upload_chunk_cleanup_queue
                              SET expires_at=updated + ?''',
                           (retention_days * 86400,))
            self.chunk_cleanup_enabled = enabled
            self.chunk_retention_days = retention_days
        if hasattr(self, '_chunk_cleanup_wakeup'):
            self._chunk_cleanup_wakeup.set()
        return self.chunk_cleanup_settings()

    def cleanup_expired_chunks(self):
        with self._config_lock:
            enabled = bool(self.chunk_cleanup_enabled)
        if not enabled:
            return dict(deleted=0, freedBytes=0, skipped=0, enabled=False)
        now, deleted, freed_bytes, skipped = time.time(), 0, 0, 0
        delete_failures = 0
        first_delete_error = None
        while not self._closed.is_set():
            with self.db() as db:
                rows = db.execute('''SELECT identifier,chunk_bytes,expires_at
                    FROM upload_chunk_cleanup_queue WHERE expires_at<=?
                    ORDER BY expires_at LIMIT 100''',
                                  (now,)).fetchall()
            if not rows:
                break
            made_progress = False
            for candidate in rows:
                identifier = candidate['identifier']
                with self._lock(identifier):
                    with self.db() as db:
                        queued = db.execute('''SELECT expires_at,chunk_bytes
                            FROM upload_chunk_cleanup_queue WHERE identifier=?''',
                                            (identifier,)).fetchone()
                        current = db.execute('SELECT status FROM uploads WHERE identifier=?',
                                             (identifier,)).fetchone()
                    if not queued or queued['expires_at'] > time.time():
                        skipped += 1
                        continue
                    if current and current['status'] != 'uploading':
                        with self.db() as db:
                            db.execute('DELETE FROM upload_chunk_cleanup_queue WHERE identifier=?',
                                       (identifier,))
                        made_progress = True
                        skipped += 1
                        continue
                    session = self._session(identifier)
                    disk_bytes = 0
                    try:
                        if session.is_dir():
                            disk_bytes = sum(path.stat().st_size for path in session.rglob('*')
                                             if path.is_file())
                            shutil.rmtree(session)
                        with self.db() as db:
                            removed = db.execute('''DELETE FROM uploads WHERE identifier=?
                                                    AND status='uploading' ''',
                                                 (identifier,)).rowcount
                            db.execute('DELETE FROM upload_chunk_cleanup_queue WHERE identifier=?',
                                       (identifier,))
                        made_progress = True
                        if removed or not current:
                            deleted += 1
                            freed_bytes += disk_bytes or int(queued['chunk_bytes'] or 0)
                        else:
                            skipped += 1
                    except OSError as exc:
                        skipped += 1
                        delete_failures += 1
                        if first_delete_error is None:
                            first_delete_error = (identifier, exc)
            if len(rows) < 100 or not made_progress:
                break
        if first_delete_error:
            identifier, exc = first_delete_error
            log.warning(
                'Unable to delete %s expired upload chunk directories; first=%s: %s',
                delete_failures, identifier, exc)
        finished = time.time()
        with self.db() as db:
            for key, value in (
                    ('chunk_cleanup.last_run', finished),
                    ('chunk_cleanup.last_deleted', deleted),
                    ('chunk_cleanup.last_bytes', freed_bytes)):
                db.execute('''INSERT INTO ffmpeg_settings(key,value,updated) VALUES(?,?,?)
                              ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                              updated=excluded.updated''', (key, str(value), finished))
        return dict(deleted=deleted, freedBytes=freed_bytes, skipped=skipped,
                    enabled=True, completedAt=finished)

    def _chunk_cleanup_loop(self):
        failures = 0
        retry_delay = 0
        last_error_log = 0.0
        while not self._closed.is_set():
            if retry_delay:
                if self._closed.wait(retry_delay):
                    break
                self._chunk_cleanup_wakeup.clear()
            try:
                self.cleanup_expired_chunks()
                with self._config_lock:
                    enabled = bool(self.chunk_cleanup_enabled)
                next_expiry = None
                if enabled:
                    with self.db() as db:
                        row = db.execute(
                            'SELECT min(expires_at) expires_at FROM upload_chunk_cleanup_queue').fetchone()
                        next_expiry = row['expires_at'] if row else None
            except Exception as exc:
                failures += 1
                retry_delay = self._background_retry_delay(failures)
                now = time.monotonic()
                if (failures == 1
                        or now - last_error_log >= self.BACKGROUND_ERROR_LOG_INTERVAL):
                    log.warning(
                        'Upload chunk cleanup unavailable; retrying in %ss: %s',
                        retry_delay, exc)
                    last_error_log = now
                continue
            if failures:
                log.info('Upload chunk cleanup recovered after %s failures', failures)
            failures = 0
            retry_delay = 0
            timeout = (None if not enabled or next_expiry is None
                       else 60 if next_expiry <= time.time()
                       else max(0.1, next_expiry - time.time()))
            self._chunk_cleanup_wakeup.wait(timeout)
            self._chunk_cleanup_wakeup.clear()

    def _ffmpeg_concurrency(self, db=None):
        if db is None:
            with self.db() as connection:
                return self._ffmpeg_concurrency(connection)
        row = db.execute(
            "SELECT value FROM ffmpeg_settings WHERE key='concurrency'").fetchone()
        try:
            return max(0, min(self.ffmpeg_max_concurrency, int(row['value'] if row else 1)))
        except (TypeError, ValueError):
            return 1

    def ffmpeg_settings(self):
        with self.db() as db:
            concurrency = self._ffmpeg_concurrency(db)
            rows = db.execute('SELECT status,count(*) AS total FROM ffmpeg_tasks GROUP BY status').fetchall()
        counts = {row['status']: row['total'] for row in rows}
        running = sum(value for key, value in counts.items()
                      if key not in {'queued', 'completed', 'failed', 'cancelled'})
        return {
            'concurrency': concurrency, 'maxConcurrency': self.ffmpeg_max_concurrency,
            'paused': concurrency == 0, 'running': running,
            'queued': counts.get('queued', 0), 'completed': counts.get('completed', 0),
            'failed': counts.get('failed', 0), 'cancelled': counts.get('cancelled', 0),
        }

    def set_ffmpeg_concurrency(self, concurrency):
        concurrency = int(concurrency)
        if concurrency < 0 or concurrency > self.ffmpeg_max_concurrency:
            raise UploadError(
                400, 40022, f'FFmpeg 并发数必须为 0—{self.ffmpeg_max_concurrency}')
        with self.db() as db:
            db.execute('''INSERT INTO ffmpeg_settings(key,value,updated) VALUES('concurrency',?,?)
                          ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                          updated=excluded.updated''', (str(concurrency), time.time()))
        self._ffmpeg_wakeup.set()
        return self.ffmpeg_settings()

    def _enqueue_ffmpeg(self, identifier, file_name):
        identifier = self.identifier(identifier)
        now = time.time()
        with self.db() as db:
            db.execute('''INSERT INTO ffmpeg_tasks(
                          id,upload_identifier,file_name,status,stage,progress,message,error,
                          created,queued_at,started_at,completed_at,updated)
                          VALUES(?,?,?,'queued','queued',0,'等待 FFmpeg 调度',NULL,?,?,NULL,NULL,?)
                          ON CONFLICT(id) DO UPDATE SET file_name=excluded.file_name,
                          status='queued',stage='queued',progress=0,
                          compression_required=NULL,compression_reason=NULL,
                          message='等待 FFmpeg 调度',error=NULL,queued_at=excluded.queued_at,
                          started_at=NULL,completed_at=NULL,updated=excluded.updated''',
                       (identifier, identifier, file_name, now, now, now))
        with self._lock(identifier):
            metadata = self._read_metadata(self._session(identifier))
            metadata.update({
                'status': 'queued', 'processingStage': 'queued',
                'message': '等待 FFmpeg 调度', 'error': None, 'completed': False,
                'updatedAt': now,
            })
            self._save_metadata(metadata)
        self._ffmpeg_wakeup.set()
        return identifier

    def _ffmpeg_dispatch_loop(self):
        failures = 0
        retry_delay = 0
        last_error_log = 0.0
        while not self._closed.is_set():
            if retry_delay:
                if self._closed.wait(retry_delay):
                    break
                self._ffmpeg_wakeup.clear()
            else:
                self._ffmpeg_wakeup.wait(timeout=1)
                self._ffmpeg_wakeup.clear()
                if self._closed.is_set():
                    break
            try:
                self._dispatch_ffmpeg_tasks()
            except Exception as exc:
                failures += 1
                retry_delay = self._background_retry_delay(failures)
                now = time.monotonic()
                if (failures == 1
                        or now - last_error_log >= self.BACKGROUND_ERROR_LOG_INTERVAL):
                    log.warning(
                        'FFmpeg task dispatch unavailable; retrying in %ss: %s',
                        retry_delay, exc)
                    last_error_log = now
            else:
                if failures:
                    log.info('FFmpeg task dispatch recovered after %s failures', failures)
                failures = 0
                retry_delay = 0

    def _dispatch_ffmpeg_tasks(self):
        concurrency = self._ffmpeg_concurrency()
        if concurrency <= 0:
            return
        with self._guard:
            running = {key for key, future in self._ffmpeg_futures.items()
                       if not future.done()}
            slots = max(0, concurrency - len(running))
            if not slots:
                return
            with self.db() as db:
                rows = db.execute("""SELECT id FROM ffmpeg_tasks WHERE status='queued'
                                   ORDER BY queued_at,id LIMIT ?""", (slots + len(running),)).fetchall()
                candidates = [row['id'] for row in rows if row['id'] not in running][:slots]
                identifiers = []
                now = time.time()
                for identifier in candidates:
                    cursor = db.execute("""UPDATE ffmpeg_tasks SET status='probing',stage='probing',
                               progress=1,message='准备读取视频信息',started_at=?,updated=?
                               WHERE id=? AND status='queued'""", (now, now, identifier))
                    if cursor.rowcount:
                        identifiers.append(identifier)
            for identifier in identifiers:
                future = self._ffmpeg_executor.submit(self._run_ffmpeg_task, identifier)
                self._ffmpeg_futures[identifier] = future
                future.add_done_callback(
                    lambda _future, current=identifier: self._ffmpeg_done(current))

    def _ffmpeg_done(self, identifier):
        with self._guard:
            self._ffmpeg_futures.pop(identifier, None)
        self._ffmpeg_wakeup.set()

    def _update_ffmpeg_task(self, identifier, status=None, stage=None, progress=None,
                            message=None, error=None, compression_required=None,
                            compression_reason=None, completed=False):
        assignments, values = ['updated=?'], [time.time()]
        for column, value in (
                ('status', status), ('stage', stage), ('progress', progress),
                ('message', message), ('error', error),
                ('compression_required', None if compression_required is None
                 else int(bool(compression_required))),
                ('compression_reason', compression_reason)):
            if value is not None:
                assignments.append(f'{column}=?')
                values.append(value)
        if completed:
            assignments.append('completed_at=?')
            values.append(time.time())
        values.append(identifier)
        with self.db() as db:
            db.execute(f"UPDATE ffmpeg_tasks SET {','.join(assignments)} WHERE id=?", values)

    def list_ffmpeg_tasks(self, page=1, page_size=20, keyword=None, status=None):
        conditions, values = [], []
        if keyword:
            escaped = str(keyword).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            conditions.append("(f.file_name LIKE ? ESCAPE '\\' OR f.id LIKE ? ESCAPE '\\')")
            values.extend((f'%{escaped}%', f'%{escaped}%'))
        if status:
            if status == 'running':
                conditions.append("f.status NOT IN ('queued','completed','failed','cancelled')")
            else:
                conditions.append('f.status=?')
                values.append(status)
        clause = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
        select = '''SELECT f.*,u.duration,u.width,u.height,u.video_codec,u.audio_codec,
                    u.bit_rate,u.original_size,u.processed_size,u.compressed,
                    u.compression_ratio,u.path,u.cover_path FROM ffmpeg_tasks f
                    LEFT JOIN uploads u ON u.identifier=f.upload_identifier'''
        with self.db() as db:
            total = db.execute('SELECT count(*) FROM ffmpeg_tasks f' + clause, values).fetchone()[0]
            rows = db.execute(select + clause +
                              ' ORDER BY f.updated DESC,f.id DESC LIMIT ? OFFSET ?',
                              (*values, page_size, (page - 1) * page_size)).fetchall()
        items = []
        for row in rows:
            items.append({
                'id': row['id'], 'uploadIdentifier': row['upload_identifier'],
                'fileName': row['file_name'], 'status': row['status'],
                'stage': row['stage'], 'progress': row['progress'],
                'compressionRequired': (None if row['compression_required'] is None
                                        else bool(row['compression_required'])),
                'compressionReason': row['compression_reason'], 'message': row['message'],
                'error': row['error'], 'duration': row['duration'],
                'durationText': self._duration_text(row['duration']),
                'width': row['width'], 'height': row['height'],
                'videoCodec': row['video_codec'], 'audioCodec': row['audio_codec'],
                'bitRate': row['bit_rate'], 'originalSize': row['original_size'],
                'processedSize': row['processed_size'], 'compressed': bool(row['compressed']),
                'compressionRatio': row['compression_ratio'],
                'path': self.rebase_public_url(row['path']),
                'coverPath': self.rebase_public_url(row['cover_path']),
                'createdAt': row['created'], 'queuedAt': row['queued_at'],
                'startedAt': row['started_at'], 'completedAt': row['completed_at'],
                'updatedAt': row['updated'],
            })
        return {'items': items, 'page': page, 'pageSize': page_size, 'total': total}

    def retry_ffmpeg_tasks(self, identifiers):
        retried, skipped = [], []
        for identifier in dict.fromkeys(identifiers):
            identifier = self.identifier(identifier)
            with self.db() as db:
                task = db.execute('SELECT * FROM ffmpeg_tasks WHERE id=?', (identifier,)).fetchone()
            metadata = self._read_metadata(self._session(identifier))
            source = self._source_from_metadata(metadata)
            if not task or task['status'] not in {'failed', 'cancelled'} or source is None:
                skipped.append(identifier)
                continue
            self._enqueue_ffmpeg(identifier, task['file_name'])
            retried.append(identifier)
        return {'retried': retried, 'skipped': skipped}

    def cancel_ffmpeg_tasks(self, identifiers):
        cancelled, skipped = [], []
        now = time.time()
        for identifier in dict.fromkeys(identifiers):
            identifier = self.identifier(identifier)
            with self.db() as db:
                cursor = db.execute("""UPDATE ffmpeg_tasks SET status='cancelled',stage='cancelled',
                                  message='任务已取消',completed_at=?,updated=?
                                  WHERE id=? AND status='queued'""", (now, now, identifier))
            if not cursor.rowcount:
                skipped.append(identifier)
                continue
            with self._lock(identifier):
                metadata = self._read_metadata(self._session(identifier))
                metadata.update({'status': 'cancelled', 'processingStage': 'cancelled',
                                 'message': 'FFmpeg 任务已取消', 'updatedAt': now})
                self._save_metadata(metadata, remove_chunk_cleanup=True)
            cancelled.append(identifier)
        return {'cancelled': cancelled, 'skipped': skipped}

    def delete_ffmpeg_task(self, identifier):
        identifier = self.identifier(identifier)
        with self.db() as db:
            cursor = db.execute("""DELETE FROM ffmpeg_tasks WHERE id=?
                                AND status IN ('completed','failed','cancelled')""", (identifier,))
        return bool(cursor.rowcount)

    @staticmethod
    def identifier(value):
        value = (value or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
            raise UploadError(400, 40010, 'identifier 只能包含字母、数字、下划线和连字符')
        return value

    @staticmethod
    def filename(value):
        value = Path((value or '').replace('\\', '/')).name.strip()
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).rstrip(' .')
        if value in ('', '.', '..'):
            raise UploadError(400, 40011, '文件名无效')
        if len(value) > 180:
            suffix = Path(value).suffix[:20]
            value = Path(value).stem[:160 - len(suffix)] + suffix
        return value

    @classmethod
    def safe_filename(cls, value):
        value = cls.filename(value)
        lower_name = value.lower()
        suffixes = {suffix.lower() for suffix in Path(value).suffixes}
        blocked = sorted(suffixes & cls.BLOCKED_EXTENSIONS)
        if blocked or lower_name in {'.env', '.htaccess'}:
            file_type = blocked[0] if blocked else lower_name
            raise UploadError(415, 41510, f'不允许上传 {file_type} 类型的文件')
        return value

    @staticmethod
    def validate_file_content(path):
        """Reject executable or script content even when its extension was disguised."""
        with path.open('rb') as source:
            head = source.read(8192)
        lowered = head.lstrip().lower()
        executable_magic = (
            head.startswith(b'MZ'),
            head.startswith(b'\x7fELF'),
            head.startswith((b'\xfe\xed\xfa\xce', b'\xfe\xed\xfa\xcf',
                             b'\xce\xfa\xed\xfe', b'\xcf\xfa\xed\xfe')),
        )
        script_markers = (
            lowered.startswith(b'#!'),
            b'<?php' in lowered,
            b'<%@ page' in lowered,
            b'<jsp:' in lowered,
        )
        if any(executable_magic) or any(script_markers):
            raise UploadError(415, 41511, '检测到可执行文件或脚本内容，禁止上传')

    def _shard_parts(self, identifier):
        identifier = self.identifier(identifier)
        key = identifier.casefold()
        if len(key) < 4:
            key = hashlib.sha256(identifier.encode()).hexdigest()
        return key[:2], key[2:4]

    def _shard_root(self, root, identifier):
        first, second = self._shard_parts(identifier)
        return root / first / second

    def _session(self, identifier):
        identifier = self.identifier(identifier)
        return self._shard_root(self.chunks_root, identifier) / identifier

    def _video_directory(self, identifier):
        identifier = self.identifier(identifier)
        return self._shard_root(self.category_roots['video'], identifier) / identifier

    def _single_file_target(self, name, identifier):
        name = self.filename(name)
        identifier = self.identifier(identifier)
        category = self.file_type(name)
        suffix = ''.join(Path(name).suffixes)
        return self._shard_root(self.category_roots[category], identifier) / f'{identifier}{suffix}'

    def _remove_public_resources(self, identifier):
        identifier = self.identifier(identifier)
        removed = []
        video = self._video_directory(identifier)
        if video.is_dir():
            shutil.rmtree(video)
            removed.append('video')
        for category in ('image', 'other'):
            leaf = self._shard_root(self.category_roots[category], identifier)
            for target in leaf.glob(f'{identifier}.*'):
                if target.is_file():
                    target.unlink()
                    removed.append(category)
            plain = leaf / identifier
            if plain.is_file():
                plain.unlink()
                removed.append(category)
        return removed

    def _metadata_from_row(self, row):
        if not row:
            return {}
        try:
            warnings = json.loads(row['warnings'] or '[]')
        except ValueError:
            warnings = []
        result = {
            'identifier': row['identifier'], 'fileName': row['file_name'],
            'fileType': row['file_type'], 'mediaType': row['media_type'],
            'size': row['size'], 'totalChunks': row['total_chunks'],
            'uploadedCount': row['uploaded_count'], 'completed': bool(row['completed']),
            'originalDeleted': bool(row['original_deleted']), 'status': row['status'],
            'processingStage': row['processing_stage'], 'message': row['message'],
            'error': row['error'], 'warnings': warnings, 'path': row['path'],
            'hlsPath': row['hls_path'], 'coverPath': row['cover_path'],
            'posterPath': row['poster_path'], 'previewPath': row['preview_path'],
            'vttPath': row['vtt_path'], 'previewM3u8Path': row['preview_m3u8_path'],
            'preview5M3u8Path': row['preview5_m3u8_path'], 'sourceFile': row['source_file'],
            'duration': row['duration'], 'durationText': self._duration_text(row['duration']),
            'width': row['width'], 'height': row['height'],
            'isPortrait': bool(row['is_portrait']), 'imageFormat': row['image_format'],
            'videoCodec': row['video_codec'], 'audioCodec': row['audio_codec'],
            'bitRate': row['bit_rate'], 'originalSize': row['original_size'],
            'processedSize': row['processed_size'], 'compressed': bool(row['compressed']),
            'compressionRatio': row['compression_ratio'],
            'r2SyncEnabled': bool(row['r2_sync_enabled']),
            'r2Synced': bool(row['r2_synced']),
            'r2SyncStatus': row['r2_sync_status'] or ('synced' if row['r2_synced'] else 'unsynced'),
            'r2SyncedAt': row['r2_synced_at'],
            'localDeleted': bool(row['local_deleted']),
            'localDeletedAt': row['local_deleted_at'],
            'createdAt': row['created'], 'updatedAt': row['updated'],
        }
        for field in ('path', 'hlsPath', 'coverPath', 'posterPath', 'previewPath',
                      'vttPath', 'previewM3u8Path', 'preview5M3u8Path'):
            result[field] = self.rebase_public_url(result.get(field))
        return result

    def _read_metadata(self, session):
        identifier = self.identifier(session.name)
        with self.db() as db:
            return self._metadata_from_row(
                db.execute('SELECT * FROM uploads WHERE identifier=?', (identifier,)).fetchone())

    def _save_metadata(self, data, chunk_expiry=None, remove_chunk_cleanup=False):
        identifier = self.identifier(data['identifier'])
        now = time.time()
        values = (
            identifier, data.get('fileName'), data.get('fileType'), data.get('mediaType'),
            int(data.get('size') or 0), data.get('totalChunks'),
            int(data.get('uploadedCount') or 0), int(bool(data.get('completed'))),
            int(bool(data.get('originalDeleted'))), data.get('status') or 'uploading',
            data.get('processingStage'), data.get('message'), data.get('error'),
            json.dumps(data.get('warnings') or [], ensure_ascii=False), data.get('path'),
            data.get('hlsPath'), data.get('coverPath'), data.get('posterPath'),
            data.get('previewPath'), data.get('vttPath'), data.get('previewM3u8Path'),
            data.get('preview5M3u8Path'), data.get('sourceFile'), data.get('duration'),
            data.get('width'), data.get('height'), int(bool(data.get('isPortrait'))),
            data.get('imageFormat'), data.get('videoCodec'),
            data.get('audioCodec'), data.get('bitRate'), data.get('originalSize'),
            data.get('processedSize'), int(bool(data.get('compressed'))),
            data.get('compressionRatio'), int(bool(data.get('r2SyncEnabled'))),
            int(bool(data.get('r2Synced'))),
            data.get('r2SyncStatus') or ('synced' if data.get('r2Synced') else 'unsynced'),
            data.get('r2SyncedAt'), int(bool(data.get('localDeleted'))),
            data.get('localDeletedAt'),
            data.get('createdAt') or now, data.get('updatedAt') or now,
        )
        with self.db() as db:
            db.execute(f'''INSERT INTO uploads(identifier,file_name,file_type,media_type,size,
                total_chunks,uploaded_count,completed,original_deleted,status,processing_stage,
                message,error,warnings,path,hls_path,cover_path,poster_path,preview_path,vtt_path,
                preview_m3u8_path,preview5_m3u8_path,source_file,duration,width,height,
                is_portrait,image_format,video_codec,audio_codec,bit_rate,
                original_size,processed_size,compressed,
                compression_ratio,r2_sync_enabled,r2_synced,r2_sync_status,r2_synced_at,
                local_deleted,local_deleted_at,created,updated)
                VALUES({','.join('?' for _ in values)})
                ON CONFLICT(identifier) DO UPDATE SET
                file_name=excluded.file_name,file_type=excluded.file_type,
                media_type=excluded.media_type,size=excluded.size,total_chunks=excluded.total_chunks,
                uploaded_count=excluded.uploaded_count,completed=excluded.completed,
                original_deleted=excluded.original_deleted,status=excluded.status,
                processing_stage=excluded.processing_stage,message=excluded.message,error=excluded.error,
                warnings=excluded.warnings,path=excluded.path,hls_path=excluded.hls_path,
                cover_path=excluded.cover_path,poster_path=excluded.poster_path,
                preview_path=excluded.preview_path,vtt_path=excluded.vtt_path,
                preview_m3u8_path=excluded.preview_m3u8_path,
                preview5_m3u8_path=excluded.preview5_m3u8_path,
                source_file=excluded.source_file,duration=excluded.duration,
                width=excluded.width,height=excluded.height,
                is_portrait=excluded.is_portrait,image_format=excluded.image_format,
                video_codec=excluded.video_codec,
                audio_codec=excluded.audio_codec,bit_rate=excluded.bit_rate,
                original_size=excluded.original_size,processed_size=excluded.processed_size,
                compressed=excluded.compressed,compression_ratio=excluded.compression_ratio,
                r2_sync_enabled=excluded.r2_sync_enabled,
                r2_synced=excluded.r2_synced,
                r2_sync_status=excluded.r2_sync_status,
                r2_synced_at=excluded.r2_synced_at,
                local_deleted=excluded.local_deleted,
                local_deleted_at=excluded.local_deleted_at,
                updated=excluded.updated''', values)
            if chunk_expiry is not None:
                db.execute('''INSERT INTO upload_chunk_cleanup_queue(
                    identifier,expires_at,chunk_bytes,uploaded_count,updated)
                    VALUES(?,?,?,?,?) ON CONFLICT(identifier) DO UPDATE SET
                    expires_at=excluded.expires_at,chunk_bytes=excluded.chunk_bytes,
                    uploaded_count=excluded.uploaded_count,updated=excluded.updated''',
                           (identifier, float(chunk_expiry), int(data.get('size') or 0),
                            int(data.get('uploadedCount') or 0), now))
            elif remove_chunk_cleanup:
                db.execute('DELETE FROM upload_chunk_cleanup_queue WHERE identifier=?',
                           (identifier,))

    @staticmethod
    def _chunk_numbers(session):
        numbers = []
        if session.is_dir():
            for path in session.glob('*.part'):
                if re.fullmatch(r'\d{6}\.part', path.name):
                    numbers.append(int(path.stem))
        return sorted(numbers)

    @staticmethod
    def _write_upload(upload, target, limit, hasher=None):
        size = 0
        try:
            with target.open('xb') as output:
                while True:
                    block = upload.file.read(UploadManager.BUFFER_SIZE)
                    if not block:
                        break
                    size += len(block)
                    if size > limit:
                        raise UploadError(413, 41301, '上传文件超过大小限制')
                    if hasher is not None:
                        hasher.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
        except Exception as exc:
            target.unlink(missing_ok=True)
            raise
        if size == 0:
            target.unlink(missing_ok=True)
            raise UploadError(400, 40012, '上传文件不能为空')
        return size

    def _result(self, path, size, identifier=None, metadata=None):
        relative = path.relative_to(self.files_root).as_posix()
        display_name = (metadata or {}).get('fileName') or path.name
        media_type = ((metadata or {}).get('mediaType') or mimetypes.guess_type(display_name)[0]
                      or 'application/octet-stream')
        result = {
            'path': self.rebase_public_url((metadata or {}).get('path')) or self.public_url(relative),
            'fileName': display_name,
            'fileType': (metadata or {}).get('fileType') or self.file_type(display_name, media_type),
            'size': size,
            'mediaType': media_type,
            'previewType': self.preview_type(display_name, media_type),
            'originalDeleted': bool((metadata or {}).get('originalDeleted')),
            'status': 'completed',
            'completed': True,
        }
        if identifier:
            result['identifier'] = identifier
        if metadata and metadata.get('hlsPath'):
            result.update({
                'hlsPath': self.rebase_public_url(metadata['hlsPath']),
                'segmented': True,
                'segmentDuration': self.hls_time,
            })
        if metadata:
            for field in ('coverPath', 'posterPath', 'previewPath', 'vttPath',
                          'previewM3u8Path', 'preview5M3u8Path'):
                if metadata.get(field):
                    result[field] = self.rebase_public_url(metadata[field])
            result['processingStage'] = metadata.get('processingStage') or metadata.get('status')
            result['warnings'] = metadata.get('warnings') or []
            for field in ('duration', 'durationText', 'width', 'height', 'videoCodec',
                          'isPortrait', 'imageFormat', 'audioCodec', 'bitRate',
                          'originalSize', 'processedSize',
                          'compressed', 'compressionRatio'):
                result[field] = metadata.get(field)
        return result

    @staticmethod
    def preview_type(name, media_type=None):
        media_type = media_type or mimetypes.guess_type(name)[0] or ''
        suffix = Path(name).suffix.lower()
        if media_type.startswith('video/') or suffix in {'.mkv', '.m4v', '.mov', '.avi', '.webm'}:
            return 'video'
        if media_type.startswith('image/'):
            return 'image'
        if media_type.startswith('audio/'):
            return 'audio'
        if media_type == 'application/pdf' or suffix == '.pdf':
            return 'pdf'
        return 'file'

    @classmethod
    def file_type(cls, name, media_type=None):
        preview = cls.preview_type(name, media_type)
        if preview == 'video':
            return 'video'
        if preview == 'image':
            return 'image'
        return 'other'

    def public_url(self, relative):
        path = quote(str(relative).replace('\\', '/').lstrip('/'), safe='/')
        base = (self._public_base_url_getter() if self._public_base_url_getter
                else self.public_base_url)
        return f'{base.rstrip("/")}/uploads/{path}'

    def rebase_public_url(self, value):
        if not value:
            return value
        try:
            parsed = urlsplit(str(value))
            path = parsed.path if parsed.scheme or parsed.netloc else str(value).split('?', 1)[0]
            if not path.startswith('/uploads/'):
                return value
            suffix = ('?' + parsed.query) if parsed.query else ''
            base = (self._public_base_url_getter() if self._public_base_url_getter
                    else self.public_base_url)
            return base.rstrip('/') + path + suffix
        except (TypeError, ValueError):
            return value

    def _run_media_command(self, command, timeout=None, runtime_identifier=None,
                           runtime_stage=None):
        if runtime_identifier:
            process = None
            try:
                process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE)
                self._runtime_started(runtime_identifier, process, runtime_stage)
                stdout, stderr = process.communicate(timeout=timeout or self.video_timeout)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                if process:
                    process.kill()
                    process.wait()
                return None
            except OSError:
                return None
            finally:
                if process:
                    self._runtime_finished(runtime_identifier, process.pid)
        try:
            return subprocess.run(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=timeout or self.video_timeout, check=False)
        except (subprocess.TimeoutExpired, OSError):
            return None

    def _run_ffmpeg_media_command(self, command, duration, progress_callback,
                                  runtime_identifier=None, runtime_stage=None):
        command = list(command)
        if command and Path(command[0]).name.lower().startswith('ffmpeg'):
            command[5:5] = ['-progress', 'pipe:1', '-nostats']
        started = time.monotonic()
        last_update = 0.0
        progress_failures = 0
        progress_retry_at = 0.0
        progress_last_error_log = 0.0
        def emit(percent):
            nonlocal progress_failures, progress_retry_at, progress_last_error_log
            now = time.monotonic()
            if now < progress_retry_at:
                return
            try:
                progress_callback(percent)
            except Exception as exc:
                progress_failures += 1
                retry_delay = self._background_retry_delay(progress_failures)
                progress_retry_at = now + retry_delay
                if (progress_failures == 1
                        or now - progress_last_error_log >= self.BACKGROUND_ERROR_LOG_INTERVAL):
                    log.warning(
                        'FFmpeg progress persistence unavailable; retrying in %ss: %s',
                        retry_delay, exc)
                    progress_last_error_log = now
            else:
                if progress_failures:
                    log.info(
                        'FFmpeg progress persistence recovered after %s failures',
                        progress_failures)
                progress_failures = 0
                progress_retry_at = 0.0
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            self._runtime_started(runtime_identifier, process, runtime_stage)
            while True:
                line = process.stdout.readline() if process.stdout else b''
                if line:
                    key, _, raw_value = line.decode('utf-8', errors='ignore').strip().partition('=')
                    if key == 'frame':
                        self._runtime_updated(runtime_identifier, frame=self._number(raw_value))
                    elif key == 'speed':
                        self._runtime_updated(runtime_identifier, speed=raw_value)
                    elif key == 'out_time':
                        self._runtime_updated(runtime_identifier, outTime=raw_value)
                    if key in {'out_time_ms', 'out_time_us'} and duration:
                        value = self._number(raw_value)
                        if value is not None:
                            now = time.monotonic()
                            if now - last_update >= 0.75:
                                emit(min(100.0, value / 1_000_000 / duration * 100))
                                last_update = now
                if process.poll() is not None:
                    break
                if time.monotonic() - started > self.video_timeout:
                    process.kill()
                    process.wait()
                    return None
            stderr = process.stderr.read() if process.stderr else b''
            if process.returncode == 0:
                emit(100.0)
            return subprocess.CompletedProcess(command, process.returncode, b'', stderr)
        except OSError:
            return None
        finally:
            if 'process' in locals():
                self._runtime_finished(runtime_identifier, process.pid)

    @staticmethod
    def _duration_text(duration):
        if not duration:
            return None
        seconds = max(0, round(float(duration)))
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        return f'{hours:02d}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:02d}:{seconds:02d}'

    @staticmethod
    def _number(value, converter=int):
        try:
            number = converter(value)
            return number if math.isfinite(number) and number >= 0 else None
        except (TypeError, ValueError, OverflowError):
            return None

    def _probe_media(self, source):
        result = self._run_media_command([
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration,size,bit_rate:'
                             'stream=codec_type,codec_name,width,height,bit_rate:'
                             'stream_tags=rotate:stream_side_data=rotation',
            '-of', 'json', str(source),
        ], min(self.video_timeout, 60))
        if not result or result.returncode:
            return None
        try:
            payload = json.loads(result.stdout.decode('utf-8', errors='ignore'))
        except (ValueError, TypeError):
            return None
        video = next((item for item in payload.get('streams', [])
                      if item.get('codec_type') == 'video'), {})
        audio = next((item for item in payload.get('streams', [])
                      if item.get('codec_type') == 'audio'), {})
        format_data = payload.get('format') or {}
        duration = self._number(format_data.get('duration'), float)
        original_size = self._number(format_data.get('size')) or source.stat().st_size
        total_bit_rate = self._number(format_data.get('bit_rate'))
        video_bit_rate = self._number(video.get('bit_rate'))
        width, height = self._display_dimensions(video)
        if not video_bit_rate and duration:
            calculated = round(original_size * 8 / duration)
            audio_bit_rate = self._number(audio.get('bit_rate')) or (128_000 if audio else 0)
            video_bit_rate = max(calculated - audio_bit_rate, 0)
        return {
            'duration': duration if duration and duration > 0 else None,
            'durationText': self._duration_text(duration),
            'width': width,
            'height': height,
            'isPortrait': bool(width and height and height > width),
            'videoCodec': video.get('codec_name'), 'audioCodec': audio.get('codec_name'),
            'bitRate': video_bit_rate or total_bit_rate,
            'originalSize': original_size,
        }

    def _display_dimensions(self, stream):
        width = self._number(stream.get('width'))
        height = self._number(stream.get('height'))
        rotation = (stream.get('tags') or {}).get('rotate')
        if rotation is None:
            rotation = next((item.get('rotation') for item in stream.get('side_data_list', [])
                             if item.get('rotation') is not None), None)
        try:
            rotated = abs(round(float(rotation))) % 180 == 90
        except (TypeError, ValueError, OverflowError):
            rotated = False
        return (height, width) if rotated else (width, height)

    def _probe_image(self, source):
        result = self._run_media_command([
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name,width,height:'
                             'stream_tags=rotate:stream_side_data=rotation',
            '-of', 'json', str(source),
        ], min(self.video_timeout, 30))
        if not result or result.returncode:
            return {}
        try:
            payload = json.loads(result.stdout.decode('utf-8', errors='ignore'))
        except (ValueError, TypeError):
            return {}
        stream = next(iter(payload.get('streams') or []), {})
        width, height = self._display_dimensions(stream)
        return {
            'width': width,
            'height': height,
            'isPortrait': bool(width and height and height > width),
            'imageFormat': stream.get('codec_name'),
            'originalSize': source.stat().st_size,
        }

    def _compress_image(self, source):
        config = self._image_compression_snapshot()
        original_size = source.stat().st_size
        details = {
            'originalSize': original_size,
            'processedSize': original_size,
            'compressed': False,
            'compressionRatio': None,
            'warnings': [],
        }
        if not config['enabled'] or original_size <= config['minBytes']:
            return details

        suffix = source.suffix.lower()
        if suffix == '.gif':
            details['warnings'].append('GIF 动图不参与压缩，已保留原图')
            return details
        formats = {
            '.jpg': ('jpg', ['-q:v', str(max(
                2, min(31, round(31 - config['quality'] / 100 * 29))))]),
            '.jpeg': ('jpg', ['-q:v', str(max(
                2, min(31, round(31 - config['quality'] / 100 * 29))))]),
            '.jpe': ('jpg', ['-q:v', str(max(
                2, min(31, round(31 - config['quality'] / 100 * 29))))]),
            '.jfif': ('jpg', ['-q:v', str(max(
                2, min(31, round(31 - config['quality'] / 100 * 29))))]),
            '.png': ('png', ['-c:v', 'png', '-compression_level', '9']),
            '.webp': ('webp', [
                '-c:v', 'libwebp', '-q:v', str(config['quality']),
                '-compression_level', '4',
            ]),
        }
        selected = formats.get(suffix)
        if selected is None:
            details['warnings'].append('当前图片格式不支持压缩，已保留原图')
            return details

        output_suffix, output_options = selected
        temporary = source.with_name(
            f'.image-compress-{uuid.uuid4().hex}.{output_suffix}')
        command = [
            'ffmpeg', '-nostdin', '-y', '-loglevel', 'error', '-i', str(source),
            '-frames:v', '1', *output_options, str(temporary),
        ]
        try:
            result = self._run_media_command(command, min(self.video_timeout, 300))
            if (not result or result.returncode or not temporary.is_file()
                    or temporary.stat().st_size <= 0):
                raise OSError('ffmpeg image compression failed')
            self.validate_file_content(temporary)
            probe = self._probe_image(temporary)
            if not probe.get('width') or not probe.get('height'):
                raise OSError('compressed image validation failed')
            processed_size = temporary.stat().st_size
            if processed_size >= original_size:
                details['warnings'].append('压缩后体积没有减小，已保留原图')
                return details
            os.replace(temporary, source)
            details.update({
                'processedSize': processed_size,
                'compressed': True,
                'compressionRatio': round(
                    max(0, 1 - processed_size / original_size) * 100, 2),
            })
            return details
        except Exception:
            log.exception('Unable to compress uploaded image %s', source)
            details['warnings'].append('图片压缩失败，已保留原图')
            return details
        finally:
            temporary.unlink(missing_ok=True)

    def _process_image(self, source):
        compression = self._compress_image(source)
        metadata = self._probe_image(source)
        metadata.update(compression)
        return metadata

    def _compression_reasons(self, media, config=None):
        config = config or self._compression_snapshot()
        if not config['enabled']:
            return []
        reasons = []
        width, height = media.get('width') or 0, media.get('height') or 0
        if (media.get('videoCodec') or '').lower() != 'h264':
            reasons.append('视频编码不是 H.264')
        if media.get('audioCodec') and media['audioCodec'].lower() != 'aac':
            reasons.append('音频编码不是 AAC')
        if width > config['maxWidth'] or height > config['maxHeight']:
            reasons.append(f"分辨率超过 {config['maxWidth']}×{config['maxHeight']}")
        if config.get('compatibilityOnly'):
            return reasons
        large_or_long = ((media.get('originalSize') or 0) >= config['minBytes']
                         or (media.get('duration') or 0) >= config['minDuration'])
        excessive_bitrate = (media.get('bitRate') or 0) > config['maxVideoBitrate']
        if large_or_long and excessive_bitrate:
            reasons.append(
                f"大文件或长视频码率超过 {config['maxVideoBitrate'] / 1_000_000:g} Mbps")
        return reasons

    def _should_compress(self, media, config=None):
        return bool(self._compression_reasons(media, config))

    def _set_stage(self, identifier, status, message):
        session = self._session(identifier)
        with self._lock(identifier):
            metadata = self._read_metadata(session)
            metadata.update({
                'identifier': identifier,
                'status': status,
                'processingStage': status,
                'message': message,
                'updatedAt': time.time(),
            })
            self._save_metadata(metadata)

    def _store_media_details(self, identifier, media):
        with self._lock(identifier):
            metadata = self._read_metadata(self._session(identifier))
            metadata.update(media)
            metadata['updatedAt'] = time.time()
            self._save_metadata(metadata)

    @staticmethod
    def _vtt_time(seconds):
        milliseconds = max(0, round(seconds * 1000))
        hours, milliseconds = divmod(milliseconds, 3600000)
        minutes, milliseconds = divmod(milliseconds, 60000)
        whole_seconds, milliseconds = divmod(milliseconds, 1000)
        return f'{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}'

    def _generate_cover(self, source, directory, duration, identifier=None):
        seek = min(max((duration or 0) * 0.1, 0), max((duration or 0) - 0.2, 0))
        cover = directory / 'cover.jpg'
        command = [
            'ffmpeg', '-nostdin', '-y', '-loglevel', 'error',
            '-ss', f'{seek:.3f}', '-i', str(source), '-frames:v', '1',
            '-vf', 'scale=640:360:force_original_aspect_ratio=decrease,'
                   'pad=640:360:(ow-iw)/2:(oh-ih)/2:color=black',
            '-q:v', '3', str(cover),
        ]
        result = self._run_media_command(
            command, runtime_identifier=identifier, runtime_stage='generating_cover')
        if not result or result.returncode or not cover.is_file() or cover.stat().st_size == 0:
            cover.unlink(missing_ok=True)
            return False
        shutil.copyfile(cover, directory / 'poster.jpg')
        return True

    def _preview_segments(self, duration):
        if not duration or duration <= 0:
            return [(0.0, 3.0)]
        if duration < 8:
            return [(0.0, max(0.5, min(duration, 3.0)))]
        if duration < 30:
            length = min(3.0, duration)
            return [(min(duration * fraction, max(duration - length, 0)), length)
                    for fraction in (0.15, 0.45, 0.75)]
        return [(min(duration * fraction, duration - 3.0), 3.0)
                for fraction in (0.1, 0.3, 0.5, 0.7, 0.9)]

    def _generate_hover_preview(self, source, directory, duration, identifier=None):
        segments = self._preview_segments(duration)
        command = ['ffmpeg', '-nostdin', '-y', '-loglevel', 'error']
        for start, length in segments:
            command.extend(['-ss', f'{start:.3f}', '-t', f'{length:.3f}', '-i', str(source)])
        filters = []
        labels = []
        for index in range(len(segments)):
            label = f'v{index}'
            filters.append(
                f'[{index}:v:0]fps=15,scale=320:180:force_original_aspect_ratio=decrease,'
                f'pad=320:180:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,'
                f'setpts=PTS-STARTPTS[{label}]')
            labels.append(f'[{label}]')
        filters.append(''.join(labels) + f'concat=n={len(labels)}:v=1:a=0[outv]')
        preview = directory / 'preview_image.mp4'
        command.extend([
            '-filter_complex', ';'.join(filters), '-map', '[outv]', '-an',
            '-c:v', 'libx264', '-preset', 'superfast', '-crf', '28',
            '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(preview),
        ])
        result = self._run_media_command(
            command, runtime_identifier=identifier, runtime_stage='generating_preview')
        if not result or result.returncode or not preview.is_file() or preview.stat().st_size == 0:
            preview.unlink(missing_ok=True)
            return False
        return True

    def _generate_vtt_preview(self, source, directory, duration, identifier=None):
        if not duration:
            return False
        sheet_pattern = directory / '.preview-sheet-%03d.jpg'
        video_filter = (
            f'fps=1/{self.preview_interval},'
            'scale=200:112:force_original_aspect_ratio=decrease,'
            'pad=200:112:(ow-iw)/2:(oh-ih)/2:color=black,'
            'tile=10x10')
        result = self._run_media_command([
            'ffmpeg', '-nostdin', '-y', '-loglevel', 'error', '-i', str(source),
            '-vf', video_filter, '-q:v', '5', '-vsync', 'vfr', str(sheet_pattern),
        ], runtime_identifier=identifier, runtime_stage='generating_vtt')
        sheets = sorted(directory.glob('.preview-sheet-*.jpg'))
        if not result or result.returncode or not sheets:
            for sheet in sheets:
                sheet.unlink(missing_ok=True)
            return False
        names = []
        for index, sheet in enumerate(sheets, 1):
            name = 'preview.jpg' if index == 1 else f'preview-{index:03d}.jpg'
            os.replace(sheet, directory / name)
            names.append(name)
        frame_count = min(math.ceil(duration / self.preview_interval), len(names) * 100)
        cues = ['WEBVTT', '']
        for index in range(frame_count):
            start = index * self.preview_interval
            end = min((index + 1) * self.preview_interval, duration)
            sheet = names[index // 100]
            cell = index % 100
            x, y = (cell % 10) * 200, (cell // 10) * 112
            cues.extend([
                f'{self._vtt_time(start)} --> {self._vtt_time(end)}',
                f'{sheet}#xywh={x},{y},200,112',
                '',
            ])
        (directory / 'preview.vtt').write_text('\n'.join(cues), encoding='utf-8')
        return frame_count > 0

    def process_video(self, source, identifier, task_progress=None, compression_config=None):
        identifier = self.identifier(identifier)
        config = compression_config or self._compression_snapshot()
        target_dir = self._video_directory(identifier)
        temporary = self.files_root / ('.video-' + uuid.uuid4().hex)
        temporary.mkdir()
        warnings = []
        def notify(stage, progress, message):
            if task_progress:
                task_progress(stage, progress, message)
        try:
            notify('probing', 1, '正在读取视频时长、分辨率和码率')
            self._set_stage(identifier, 'probing', '正在读取视频时长、分辨率和码率')
            media = self._probe_media(source)
            if not media or not media.get('videoCodec'):
                raise UploadError(422, 42211, '无法读取视频信息，请检查视频编码或文件完整性')
            self._store_media_details(identifier, media)
            duration = media.get('duration')
            compression_reasons = self._compression_reasons(media, config)
            compressed = bool(compression_reasons)
            self._update_ffmpeg_task(
                identifier, compression_required=compressed,
                compression_reason='；'.join(compression_reasons) if compression_reasons else '无需压缩')
            common = [
                '-hls_time', str(config['hlsTime']), '-hls_playlist_type', 'vod',
                '-hls_segment_filename', str(temporary / '%04d.ts'),
                str(temporary / 'index.m3u8'),
            ]
            if compressed:
                notify('compressing', 5, '正在压缩视频并生成 HLS 切片')
                self._set_stage(identifier, 'compressing', '正在压缩视频并生成 HLS 切片')
                scale = (
                    f"scale='min(iw,{config['maxWidth']})':"
                    f"'min(ih,{config['maxHeight']})':"
                    'force_original_aspect_ratio=decrease:force_divisible_by=2')
                command = [
                    'ffmpeg', '-nostdin', '-y', '-loglevel', 'error', '-i', str(source),
                    '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'libx264',
                    '-preset', config['preset'], '-crf', str(config['crf']),
                    '-maxrate', str(config['maxVideoBitrate']),
                    '-bufsize', str(config['maxVideoBitrate'] * 2),
                    '-vf', scale, '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                    '-b:a', config['audioBitrate'], '-hls_flags', 'independent_segments',
                    *common,
                ]
            else:
                notify('segmenting', 5, '正在生成 HLS 视频切片')
                self._set_stage(identifier, 'segmenting', '正在生成 HLS 视频切片')
                command = [
                    'ffmpeg', '-nostdin', '-y', '-loglevel', 'error', '-i', str(source),
                    '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy',
                    '-hls_flags', 'split_by_time', *common,
                ]
            stage = 'compressing' if compressed else 'segmenting'
            message = '正在压缩视频并生成 HLS 切片' if compressed else '正在生成 HLS 视频切片'
            result = self._run_ffmpeg_media_command(
                command, duration,
                lambda percent: notify(stage, 5 + percent * 0.73, message),
                runtime_identifier=identifier, runtime_stage=stage)
            playlist = temporary / 'index.m3u8'
            segments = list(temporary.glob('[0-9][0-9][0-9][0-9].ts'))
            if not result:
                raise UploadError(504, 50410, '视频切片超时')
            if result.returncode or not playlist.is_file() or not segments:
                raise UploadError(422, 42210, '视频切片失败，请检查视频编码或文件完整性')
            processed_size = sum(segment.stat().st_size for segment in segments)
            original_size = media.get('originalSize') or source.stat().st_size
            compression_ratio = (round(max(0, 1 - processed_size / original_size) * 100, 2)
                                 if original_size else None)

            notify('generating_cover', 80, '正在生成视频封面')
            self._set_stage(identifier, 'generating_cover', '正在生成视频封面')
            if not self._generate_cover(source, temporary, duration, identifier):
                warnings.append('视频封面生成失败')

            if config.get('generateHoverPreview'):
                notify('generating_preview', 84, '正在生成列表悬浮预览视频')
                self._set_stage(identifier, 'generating_preview', '正在生成列表悬浮预览视频')
                if not self._generate_hover_preview(source, temporary, duration, identifier):
                    warnings.append('列表悬浮预览视频生成失败')

            if config.get('generateVttPreview'):
                notify('generating_vtt', 90, '正在生成播放器进度预览')
                self._set_stage(identifier, 'generating_vtt', '正在生成播放器进度预览')
                if not duration:
                    warnings.append('无法读取视频时长，未生成播放器进度预览')
                elif not self._generate_vtt_preview(source, temporary, duration, identifier):
                    warnings.append('播放器进度预览生成失败')

            notify('generating_preview', 96, '正在生成 3 分钟和 5 分钟预览清单')
            self._set_stage(identifier, 'generating_preview', '正在生成 3 分钟和 5 分钟预览清单')
            preview_playlists = ((180, 'preview.m3u8', 'previewM3u8Path'),
                                 (300, 'preview5.m3u8', 'preview5M3u8Path'))
            generated_playlists = {}
            for seconds, name, field in preview_playlists:
                if self.preview_generator.preview_playlist(temporary, seconds, name):
                    generated_playlists[field] = name
                else:
                    warnings.append(name + ' 生成失败')

            target_dir.parent.mkdir(parents=True, exist_ok=True)
            if target_dir.exists():
                shutil.rmtree(target_dir)
            os.replace(temporary, target_dir)
            relative = target_dir.relative_to(self.files_root).as_posix()
            result = {
                'hlsPath': self.public_url(f'{relative}/index.m3u8'),
                'warnings': warnings,
                **media,
                'processedSize': processed_size,
                'compressed': compressed,
                'compressionRatio': compression_ratio,
            }
            for field, name in (
                    ('coverPath', 'cover.jpg'), ('posterPath', 'poster.jpg'),
                    ('previewPath', 'preview_image.mp4'), ('vttPath', 'preview.vtt')):
                if (target_dir / name).is_file():
                    result[field] = self.public_url(f'{relative}/{name}')
            for field, name in generated_playlists.items():
                result[field] = self.public_url(f'{relative}/{name}')
            notify('generating_preview', 99, '视频资源即将完成')
            return result
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def process_preview(self, target, identifier):
        if self.preview_type(target.name) == 'video':
            return self.process_video(target, identifier)
        if self.preview_type(target.name) == 'image':
            return self._process_image(target)
        return {}

    def _source_from_metadata(self, metadata):
        relative = metadata.get('sourceFile') if metadata else None
        if not relative:
            return None
        target = (self.files_root / str(relative)).resolve()
        identifier = str(metadata.get('identifier') or '')
        if (self.files_root not in target.parents or not target.is_file()
                or not target.name.startswith(f'.source-{identifier}-')):
            return None
        return target

    def _run_ffmpeg_task(self, identifier):
        try:
            metadata = self._read_metadata(self._session(identifier))
            source = self._source_from_metadata(metadata)
            if source is None:
                raise UploadError(404, 40412, '待处理的原视频不存在')

            def progress(stage, percent, message):
                self._update_ffmpeg_task(
                    identifier, status=stage, stage=stage,
                    progress=round(max(0, min(99, percent)), 2), message=message)

            compression_config = self._compression_snapshot()
            preview = self.process_video(
                source, identifier, progress, compression_config=compression_config)
            completed_target = self.resolve_file(preview.get('hlsPath'))
            if completed_target is None:
                raise UploadError(500, 50012, 'FFmpeg 已完成但 HLS 文件不存在')
            source.unlink(missing_ok=True)
            with self._lock(identifier):
                metadata = self._read_metadata(self._session(identifier))
                now = time.time()
                metadata.update({
                    'completed': True, 'path': preview.get('hlsPath'),
                    'hlsPath': preview.get('hlsPath'), 'coverPath': preview.get('coverPath'),
                    'posterPath': preview.get('posterPath'),
                    'previewPath': preview.get('previewPath'), 'vttPath': preview.get('vttPath'),
                    'previewM3u8Path': preview.get('previewM3u8Path'),
                    'preview5M3u8Path': preview.get('preview5M3u8Path'),
                    'duration': preview.get('duration'), 'width': preview.get('width'),
                    'height': preview.get('height'),
                    'isPortrait': bool(preview.get('isPortrait')),
                    'videoCodec': preview.get('videoCodec'),
                    'audioCodec': preview.get('audioCodec'), 'bitRate': preview.get('bitRate'),
                    'originalSize': preview.get('originalSize'),
                    'processedSize': preview.get('processedSize'),
                    'compressed': bool(preview.get('compressed')),
                    'compressionRatio': preview.get('compressionRatio'),
                    'warnings': preview.get('warnings') or [], 'originalDeleted': True,
                    'status': 'completed', 'processingStage': 'completed', 'error': None,
                    'message': '视频处理完成', 'updatedAt': now,
                })
                metadata.pop('sourceFile', None)
                self._save_metadata(metadata)
                total_chunks = int(metadata.get('totalChunks') or 0)
                session = self._session(identifier)
                for number in range(1, total_chunks + 1):
                    (session / f'{number:06d}.part').unlink(missing_ok=True)
            self._update_ffmpeg_task(
                identifier, status='completed', stage='completed', progress=100,
                message='视频处理完成', completed=True)
            self._notify_completed(metadata)
        except Exception as exc:
            message = exc.message if isinstance(exc, UploadError) else 'FFmpeg 视频处理失败'
            with self._lock(identifier):
                metadata = self._read_metadata(self._session(identifier))
                metadata.update({
                    'status': 'failed', 'processingStage': 'failed',
                    'completed': False, 'error': message, 'message': message,
                    'updatedAt': time.time(),
                })
                self._save_metadata(metadata)
            self._update_ffmpeg_task(
                identifier, status='failed', stage='failed', message=message,
                error=message, completed=True)
            if not isinstance(exc, UploadError):
                log.exception('FFmpeg upload task %s failed', identifier)

    def _available_target(self, name, identifier):
        name = self.filename(name)
        category = self.file_type(name)
        if category == 'video':
            # The original video is private and is deleted after derived files are ready.
            return self.files_root / f'.source-{identifier}-{uuid.uuid4().hex[:8]}{Path(name).suffix}'
        target = self._single_file_target(name, identifier)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def upload_file(self, upload, r2_sync_enabled=False):
        name = self.safe_filename(upload.filename or '')
        self.files_root.mkdir(parents=True, exist_ok=True)
        temporary = self.files_root / ('.upload-' + uuid.uuid4().hex + '.part')
        hasher = hashlib.blake2b(digest_size=12)
        size = self._write_upload(upload, temporary, self.max_file_bytes, hasher)
        identifier = base64.urlsafe_b64encode(hasher.digest()).decode('ascii').rstrip('=')
        with self._lock(identifier):
            existing = self._read_metadata(self._session(identifier))
            completed = (self.resolve_file(existing.get('path'))
                         if existing.get('completed') and existing.get('path') else None)
            if completed:
                temporary.unlink(missing_ok=True)
                existing['r2SyncEnabled'] = bool(r2_sync_enabled)
                self._save_metadata(existing)
                result = self._result(completed, existing.get('size') or size,
                                      identifier, existing)
                result['deduplicated'] = True
                self._notify_completed(existing)
                return result
            if existing.get('status') in {
                    'merging', 'queued', 'probing', 'compressing', 'segmenting', 'generating_cover',
                    'generating_preview', 'generating_vtt'}:
                temporary.unlink(missing_ok=True)
                existing['r2SyncEnabled'] = bool(r2_sync_enabled)
                self._save_metadata(existing)
                result = dict(existing)
                result.update({
                    'previewType': self.preview_type(
                        existing.get('fileName') or name, existing.get('mediaType')),
                    'deduplicated': True,
                })
                return result
            if existing.get('status') == 'uploading':
                temporary.unlink(missing_ok=True)
                raise UploadError(409, 40921, '文件哈希与未完成的分片上传任务冲突')
            try:
                self.validate_file_content(temporary)
                self._remove_public_resources(identifier)
                for source in self.files_root.glob(f'.source-{identifier}-*'):
                    source.unlink(missing_ok=True)
                with self._guard:
                    target = self._available_target(name, identifier)
                    os.replace(temporary, target)
                now = time.time()
                video = self.file_type(name) == 'video'
                image_metadata = (self._process_image(target)
                                  if self.file_type(name) == 'image' else {})
                metadata = {
                    'identifier': identifier, 'fileName': name, 'fileType': self.file_type(name),
                    'mediaType': mimetypes.guess_type(name)[0] or 'application/octet-stream',
                    'size': size, 'totalChunks': 0, 'uploadedCount': 1,
                    'completed': not video,
                    'status': 'queued' if video else 'completed',
                    'processingStage': 'queued' if video else 'completed',
                    'message': '等待 FFmpeg 调度' if video else '上传完成',
                    'warnings': image_metadata.get('warnings') or [],
                    'r2SyncEnabled': bool(r2_sync_enabled),
                    'createdAt': now, 'updatedAt': now,
                }
                metadata.update(image_metadata)
                if video:
                    relative = self._video_directory(identifier).relative_to(self.files_root).as_posix()
                    preview_config = self._compression_snapshot()
                    video_paths = {
                        'sourceFile': target.relative_to(self.files_root).as_posix(),
                        'path': self.public_url(f'{relative}/index.m3u8'),
                        'hlsPath': self.public_url(f'{relative}/index.m3u8'),
                        'coverPath': self.public_url(f'{relative}/cover.jpg'),
                        'posterPath': self.public_url(f'{relative}/poster.jpg'),
                        'previewM3u8Path': self.public_url(f'{relative}/preview.m3u8'),
                        'preview5M3u8Path': self.public_url(f'{relative}/preview5.m3u8'),
                    }
                    if preview_config['generateHoverPreview']:
                        video_paths['previewPath'] = self.public_url(
                            f'{relative}/preview_image.mp4')
                    if preview_config['generateVttPreview']:
                        video_paths['vttPath'] = self.public_url(f'{relative}/preview.vtt')
                    metadata.update(video_paths)
                else:
                    metadata.update({
                        'path': self.public_url(target.relative_to(self.files_root).as_posix()),
                        'originalDeleted': False,
                    })
                self._save_metadata(metadata, remove_chunk_cleanup=True)
                if video:
                    self._enqueue_ffmpeg(identifier, name)
                    result = self._read_metadata(self._session(identifier))
                    result.update({
                        'previewType': 'video', 'segmented': False,
                        'deduplicated': False,
                    })
                    return result
                completed_target = target
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                if isinstance(locals().get('target'), Path):
                    target.unlink(missing_ok=True)
                self._remove_public_resources(identifier)
                shutil.rmtree(self._session(identifier), ignore_errors=True)
                with self.db() as db:
                    db.execute('DELETE FROM upload_chunk_cleanup_queue WHERE identifier=?',
                               (identifier,))
                    db.execute('DELETE FROM uploads WHERE identifier=?', (identifier,))
                if isinstance(exc, OSError):
                    raise UploadError(507, 50710, '上传存储目录不可用或磁盘写入失败') from exc
                raise
            result = self._result(completed_target, size, identifier, metadata)
            result['deduplicated'] = False
            self._notify_completed(metadata)
            return result

    def check_chunks(self, identifier, include_chunks=True):
        identifier = self.identifier(identifier)
        session = self._session(identifier)
        with self._lock(identifier):
            metadata = self._read_metadata(session)
            chunks = self._chunk_numbers(session) if include_chunks else []
            uploaded_count = (len(chunks) if include_chunks
                              else int(metadata.get('uploadedCount') or 0))
            total = metadata.get('totalChunks')
            completed_path = metadata.get('path')
            completed_file = self.resolve_file(completed_path) if metadata.get('completed') and completed_path else None
            completed = bool(completed_file) or bool(
                metadata.get('completed') and metadata.get('localDeleted') and metadata.get('r2Synced'))
            with self.db() as db:
                cleanup = db.execute('''SELECT expires_at
                    FROM upload_chunk_cleanup_queue WHERE identifier=?''',
                                     (identifier,)).fetchone()
            result = {
                'identifier': identifier,
                'uploadedChunks': chunks,
                'uploadedCount': uploaded_count,
                'totalChunks': total,
                'completed': completed,
                'path': completed_path if completed else None,
                'hlsPath': metadata.get('hlsPath'),
                'fileName': metadata.get('fileName'),
                'size': metadata.get('size'),
                'message': metadata.get('message') or ('合并完成' if completed else '等待上传'),
                'status': metadata.get('status') or ('completed' if completed else 'uploading'),
                'processingStage': metadata.get('processingStage'),
                'error': metadata.get('error'),
                'warnings': metadata.get('warnings') or [],
                'r2SyncEnabled': bool(metadata.get('r2SyncEnabled')),
                'r2Synced': bool(metadata.get('r2Synced')),
                'r2SyncStatus': metadata.get('r2SyncStatus') or ('synced' if metadata.get('r2Synced') else 'unsynced'),
                'r2SyncedAt': metadata.get('r2SyncedAt'),
                'localDeleted': bool(metadata.get('localDeleted')),
                'localDeletedAt': metadata.get('localDeletedAt'),
                'chunkExpiresAt': cleanup['expires_at'] if cleanup else None,
                'updatedAt': metadata.get('updatedAt'),
            }
            for field in ('coverPath', 'posterPath', 'previewPath', 'vttPath',
                          'previewM3u8Path', 'preview5M3u8Path', 'duration',
                          'durationText', 'width', 'height', 'isPortrait', 'imageFormat',
                          'videoCodec', 'audioCodec',
                          'bitRate', 'originalSize', 'processedSize', 'compressed',
                          'compressionRatio'):
                result[field] = metadata.get(field)
            if completed_file:
                result.update(self._result(completed_file, metadata.get('size') or completed_file.stat().st_size,
                                           identifier, metadata))
            return result

    def upload_chunk(self, identifier, chunk_number, total_chunks, upload,
                     r2_sync_enabled=False):
        identifier = self.identifier(identifier)
        if chunk_number < 1 or chunk_number > self.max_chunks:
            raise UploadError(400, 40013, 'chunkNumber 超出允许范围')
        if total_chunks is not None and (total_chunks < 1 or total_chunks > self.max_chunks):
            raise UploadError(400, 40014, 'totalChunks 超出允许范围')
        if total_chunks is not None and chunk_number > total_chunks:
            raise UploadError(400, 40015, 'chunkNumber 不能大于 totalChunks')
        session = self._session(identifier)
        with self._lock(identifier):
            session.mkdir(parents=True, exist_ok=True)
            temporary = session / ('.upload-' + uuid.uuid4().hex)
            size = self._write_upload(upload, temporary, self.max_chunk_bytes)
            chunk_path = session / f'{chunk_number:06d}.part'
            now = time.time()
            metadata = self._read_metadata(session)
            completed_path = metadata.get('path') if metadata.get('completed') else None
            completed_file = self.resolve_file(completed_path) if completed_path else None
            if completed_file:
                temporary.unlink(missing_ok=True)
                return {
                    'identifier': identifier,
                    'chunkNumber': chunk_number,
                    'totalChunks': metadata.get('totalChunks'),
                    'size': size,
                    'uploadedCount': 0,
                    'completed': True,
                    'status': 'completed',
                    'path': completed_path,
                }
            previous_total = metadata.get('totalChunks')
            if previous_total and total_chunks and previous_total != total_chunks:
                temporary.unlink(missing_ok=True)
                raise UploadError(409, 40910, 'totalChunks 与已有上传记录不一致')
            previous_size = chunk_path.stat().st_size if chunk_path.is_file() else 0
            os.replace(temporary, chunk_path)
            with self._config_lock:
                expires_at = now + int(self.chunk_retention_days) * 86400
            metadata.update({
                'identifier': identifier,
                'size': max(0, int(metadata.get('size') or 0) - previous_size + size),
                'totalChunks': total_chunks or previous_total,
                'uploadedCount': len(self._chunk_numbers(session)),
                'completed': False,
                'status': 'uploading',
                'r2SyncEnabled': bool(r2_sync_enabled),
                'error': None,
                'message': f'已上传分片 {chunk_number}',
                'createdAt': metadata.get('createdAt') or now,
                'updatedAt': now,
            })
            self._save_metadata(metadata, chunk_expiry=expires_at)
            uploaded = self._chunk_numbers(session)
            self._chunk_cleanup_wakeup.set()
        return {
            'identifier': identifier,
            'chunkNumber': chunk_number,
            'totalChunks': metadata.get('totalChunks'),
            'size': size,
            'uploadedCount': len(uploaded),
        }

    def merge(self, identifier, file_name, r2_sync_enabled=None):
        identifier = self.identifier(identifier)
        file_name = self.safe_filename(file_name)
        session = self._session(identifier)
        with self._lock(identifier):
            metadata = self._read_metadata(session)
            if not metadata:
                raise UploadError(404, 40410, '上传任务不存在')
            if r2_sync_enabled is not None:
                metadata['r2SyncEnabled'] = bool(r2_sync_enabled)
                metadata['updatedAt'] = time.time()
                self._save_metadata(metadata)
            total_chunks = int(metadata.get('totalChunks') or 0)
            if total_chunks < 1 or total_chunks > self.max_chunks:
                raise UploadError(409, 40912, '上传任务缺少有效的 totalChunks，请重新上传分片')
            if metadata.get('completed'):
                completed = self.resolve_file(metadata.get('path'))
                if completed:
                    result = self._result(
                        completed, metadata.get('size') or completed.stat().st_size,
                        identifier, metadata)
                    self._notify_completed(metadata)
                    return result
            if metadata.get('status') in {
                    'queued', 'probing', 'compressing', 'segmenting', 'generating_cover',
                    'generating_preview', 'generating_vtt'} and self._source_from_metadata(metadata):
                result = dict(metadata)
                result.update({'previewType': 'video', 'segmented': False})
                return result
            missing = [number for number in range(1, total_chunks + 1)
                       if not (session / f'{number:06d}.part').is_file()]
            if missing:
                raise UploadError(409, 40911, '文件分片不完整', {'missingChunks': missing})
            now = time.time()
            video = self.preview_type(file_name) == 'video'
            file_type = self.file_type(file_name)
            predicted_target = (self._video_directory(identifier) / 'index.m3u8' if video
                                else self._single_file_target(file_name, identifier))
            primary_path = self.public_url(predicted_target.relative_to(self.files_root).as_posix())
            hls_path = primary_path if video else None
            video_relative = self._video_directory(identifier).relative_to(self.files_root).as_posix()
            preview_config = self._compression_snapshot()
            predicted = ({
                'coverPath': self.public_url(f'{video_relative}/cover.jpg'),
                'posterPath': self.public_url(f'{video_relative}/poster.jpg'),
                'previewM3u8Path': self.public_url(f'{video_relative}/preview.m3u8'),
                'preview5M3u8Path': self.public_url(f'{video_relative}/preview5.m3u8'),
            } if video else {})
            if video and preview_config['generateHoverPreview']:
                predicted['previewPath'] = self.public_url(
                    f'{video_relative}/preview_image.mp4')
            if video and preview_config['generateVttPreview']:
                predicted['vttPath'] = self.public_url(f'{video_relative}/preview.vtt')
            metadata.update({
                'identifier': identifier, 'fileName': file_name, 'totalChunks': total_chunks,
                'fileType': file_type,
                'mediaType': mimetypes.guess_type(file_name)[0] or 'application/octet-stream',
                'status': 'merging', 'processingStage': 'merging', 'completed': False,
                'path': primary_path, 'hlsPath': hls_path, 'error': None,
                'warnings': [],
                'r2SyncEnabled': bool(metadata.get('r2SyncEnabled')),
                'message': '正在后台合并和切片' if video else '正在后台合并文件',
                'createdAt': metadata.get('createdAt') or now, 'updatedAt': now,
            })
            metadata.update(predicted)
            self._save_metadata(metadata, remove_chunk_cleanup=True)
            with self._guard:
                future = self._merge_futures.get(identifier)
                if not future or future.done():
                    future = self._executor.submit(self._run_merge, identifier)
                    self._merge_futures[identifier] = future
                    future.add_done_callback(lambda _future, current=identifier: self._merge_done(current))
            response = {
                'identifier': identifier, 'fileName': file_name, 'status': 'merging',
                'processingStage': 'merging', 'completed': False,
                'path': primary_path, 'hlsPath': hls_path,
                'fileType': file_type,
                'isPortrait': bool(metadata.get('isPortrait')),
                'previewType': 'video' if video else self.preview_type(file_name),
                'message': metadata['message'], 'warnings': [],
            }
            response.update(predicted)
            return response

    def _merge_done(self, identifier):
        with self._guard:
            self._merge_futures.pop(identifier, None)

    def _run_merge(self, identifier):
        try:
            self._merge(identifier)
        except Exception as exc:
            message = exc.message if isinstance(exc, UploadError) else '后台合并失败'
            with self._lock(identifier):
                session = self._session(identifier)
                metadata = self._read_metadata(session)
                metadata.update({'status': 'failed', 'completed': False, 'error': message,
                                 'processingStage': 'failed', 'message': message,
                                 'updatedAt': time.time()})
                self._save_metadata(metadata)
            if not isinstance(exc, UploadError):
                log.exception('Upload merge %s failed', identifier)

    def _merge(self, identifier):
        identifier = self.identifier(identifier)
        session = self._session(identifier)
        with self._lock(identifier):
            metadata = self._read_metadata(session)
            if not metadata:
                raise UploadError(404, 40410, '上传任务不存在')
            file_name = self.safe_filename(metadata.get('fileName'))
            total_chunks = int(metadata.get('totalChunks') or 0)
            if total_chunks < 1 or total_chunks > self.max_chunks:
                raise UploadError(409, 40912, '上传任务缺少有效的 totalChunks，请重新上传分片')
            completed = metadata.get('path') if metadata.get('completed') else None
            if completed:
                target = self.resolve_file(completed)
                if target:
                    result = self._result(target, target.stat().st_size,
                                          identifier, metadata)
                    self._notify_completed(metadata)
                    return result
            missing = [number for number in range(1, total_chunks + 1)
                       if not (session / f'{number:06d}.part').is_file()]
            if missing:
                raise UploadError(409, 40911, '文件分片不完整', {'missingChunks': missing})
            source_file = metadata.get('sourceFile')

        self.files_root.mkdir(parents=True, exist_ok=True)
        target = ((self.files_root / source_file).resolve()
                  if source_file else None)
        if (target is not None and
                (self.files_root not in target.parents or not target.is_file())):
            target = None
        temporary = self.files_root / ('.merge-' + uuid.uuid4().hex + '.part')
        try:
            if target is None:
                size = 0
                with temporary.open('xb') as output:
                    for number in range(1, total_chunks + 1):
                        with (session / f'{number:06d}.part').open('rb') as source:
                            while True:
                                block = source.read(self.BUFFER_SIZE)
                                if not block:
                                    break
                                size += len(block)
                                if size > self.max_file_bytes:
                                    raise UploadError(413, 41302, '合并文件超过大小限制')
                                output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                self.validate_file_content(temporary)
                with self._guard:
                    target = self._available_target(file_name, identifier)
                    os.replace(temporary, target)
                with self._lock(identifier):
                    metadata = self._read_metadata(session)
                    metadata.update({'sourceFile': target.relative_to(self.files_root).as_posix(),
                                     'size': size,
                                     'updatedAt': time.time()})
                    self._save_metadata(metadata)
            else:
                size = target.stat().st_size

            if self.file_type(file_name) == 'video':
                self._enqueue_ffmpeg(identifier, file_name)
                for number in range(1, total_chunks + 1):
                    (session / f'{number:06d}.part').unlink(missing_ok=True)
                return self._read_metadata(session)

            preview = self.process_preview(target, identifier)
            original_deleted = bool(preview.get('hlsPath'))
            if original_deleted:
                target.unlink()
                completed_target = self.resolve_file(preview['hlsPath'])
            else:
                completed_target = target
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            if isinstance(target, Path):
                target.unlink(missing_ok=True)
            self._remove_public_resources(identifier)
            if isinstance(exc, OSError):
                raise UploadError(507, 50710, '上传存储目录不可用或磁盘写入失败') from exc
            raise

        with self._lock(identifier):
            metadata = self._read_metadata(session)
            now = time.time()
            metadata.update({
                'identifier': identifier,
                'fileName': file_name,
                'totalChunks': total_chunks,
                'completed': True,
                'path': preview.get('hlsPath') or self.public_url(
                    target.relative_to(self.files_root).as_posix()),
                'size': size,
                'fileType': self.file_type(file_name),
                'hlsPath': preview.get('hlsPath'),
                'coverPath': preview.get('coverPath'),
                'posterPath': preview.get('posterPath'),
                'previewPath': preview.get('previewPath'),
                'vttPath': preview.get('vttPath'),
                'previewM3u8Path': preview.get('previewM3u8Path'),
                'preview5M3u8Path': preview.get('preview5M3u8Path'),
                'duration': preview.get('duration'),
                'durationText': preview.get('durationText'),
                'width': preview.get('width'), 'height': preview.get('height'),
                'isPortrait': bool(preview.get('isPortrait')),
                'imageFormat': preview.get('imageFormat'),
                'videoCodec': preview.get('videoCodec'),
                'audioCodec': preview.get('audioCodec'), 'bitRate': preview.get('bitRate'),
                'originalSize': preview.get('originalSize'),
                'processedSize': preview.get('processedSize'),
                'compressed': bool(preview.get('compressed')),
                'compressionRatio': preview.get('compressionRatio'),
                'warnings': preview.get('warnings') or [],
                'mediaType': mimetypes.guess_type(file_name)[0] or 'application/octet-stream',
                'originalDeleted': original_deleted,
                'status': 'completed',
                'processingStage': 'completed',
                'error': None,
                'message': '合并完成',
                'createdAt': metadata.get('createdAt') or now,
                'updatedAt': now,
            })
            metadata.pop('sourceFile', None)
            self._save_metadata(metadata)
            for number in range(1, total_chunks + 1):
                (session / f'{number:06d}.part').unlink(missing_ok=True)
            result = self._result(completed_target, size, identifier, metadata)
            self._notify_completed(metadata)
            return result

    @staticmethod
    def _relative_file(path):
        supplied = str(path or '').replace('\\', '/')
        value = unquote(urlsplit(supplied).path)
        for prefix in ('/uploads/', 'uploads/'):
            if value.startswith(prefix):
                return value[len(prefix):].lstrip('/')
        return None

    def _public_target(self, relative):
        parts = Path(str(relative).replace('\\', '/')).parts
        if not parts or parts[0] not in self.category_roots:
            return None
        category = parts[0]
        if category == 'video':
            if len(parts) < 5:
                return None
            identifier = parts[3]
        else:
            if len(parts) != 4:
                return None
            identifier = parts[3].split('.', 1)[0]
        try:
            identifier = self.identifier(identifier)
            first, second = self._shard_parts(identifier)
        except UploadError:
            return None
        if parts[1:3] != (first, second):
            return None
        target = (self.files_root / Path(*parts)).resolve()
        category_root = self.category_roots[category]
        if (category_root not in target.parents or target.name.startswith('.')
                or not target.is_file()):
            return None
        return target

    def resolve_file(self, path):
        value = self._relative_file(path)
        if value is None:
            return None
        return self._public_target(value)

    def media_path(self, relative):
        return self._public_target(unquote(relative))

    def list_uploads(self, page=1, page_size=20, keyword=None, file_type=None, status=None):
        conditions, values = [], []
        if keyword:
            keyword = str(keyword).strip()
            relative = self._relative_file(keyword)
            parts = Path(relative).parts if relative else ()
            identifier = None
            if parts and parts[0] == 'video' and len(parts) >= 4:
                identifier = parts[3]
            elif parts and parts[0] in {'image', 'other'} and len(parts) == 4:
                identifier = parts[3].split('.', 1)[0]
            try:
                identifier = self.identifier(identifier) if identifier else None
            except UploadError:
                identifier = None
            if identifier:
                conditions.append('identifier=?')
                values.append(identifier)
            else:
                escaped = keyword.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                conditions.append("(identifier=? OR file_name LIKE ? ESCAPE '\\')")
                values.extend((keyword, f'%{escaped}%'))
        if file_type:
            conditions.append('file_type=?')
            values.append(file_type)
        if status:
            conditions.append('status=?')
            values.append(status)
        clause = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
        with self.db() as db:
            total = db.execute('SELECT count(*) FROM uploads' + clause, values).fetchone()[0]
            rows = db.execute('SELECT * FROM uploads' + clause
                              + ' ORDER BY created DESC LIMIT ? OFFSET ?',
                              (*values, page_size, (page - 1) * page_size)).fetchall()
        items = []
        for row in rows:
            item = self._metadata_from_row(row)
            item.update({
                'previewType': self.preview_type(item.get('fileName') or '', item.get('mediaType')),
                'segmented': bool(item.get('hlsPath')),
                'segmentDuration': self.hls_time if item.get('hlsPath') else None,
            })
            items.append(item)
        return {'items': items, 'page': page, 'pageSize': page_size, 'total': total}

    def delete_identifier(self, identifier):
        identifier = self.identifier(identifier)
        with self._guard:
            future = self._merge_futures.get(identifier)
            if future and not future.done():
                raise UploadError(409, 40920, '上传任务正在后台处理，请完成后再删除')
            ffmpeg_future = self._ffmpeg_futures.get(identifier)
            if ffmpeg_future and not ffmpeg_future.done():
                raise UploadError(409, 40920, 'FFmpeg 任务正在执行，请完成后再删除')
            self._merge_futures.pop(identifier, None)
            self._ffmpeg_futures.pop(identifier, None)
        deleted_paths = []
        with self._lock(identifier):
            deleted_paths.extend(self._remove_public_resources(identifier))
            session = self._session(identifier)
            if session.is_dir():
                shutil.rmtree(session)
                deleted_paths.append('chunks')
            for source in self.files_root.glob(f'.source-{identifier}-*'):
                if source.is_file():
                    source.unlink()
                    deleted_paths.append('source')
            with self.db() as db:
                db.execute('DELETE FROM ffmpeg_tasks WHERE upload_identifier=?', (identifier,))
                db.execute('DELETE FROM upload_chunk_cleanup_queue WHERE identifier=?',
                           (identifier,))
                cursor = db.execute('DELETE FROM uploads WHERE identifier=?', (identifier,))
                if cursor.rowcount:
                    deleted_paths.append('record')
        return {
            'identifier': identifier,
            'deleted': bool(deleted_paths),
            'deletedPaths': sorted(set(deleted_paths)),
        }

    def delete_paths(self, paths):
        deleted, missing, failed = [], [], []
        for supplied in paths:
            value = str(supplied or '').replace('\\', '/').split('?', 1)[0]
            target = self.resolve_file(value)
            if target:
                try:
                    relative = target.relative_to(self.files_root)
                    category = relative.parts[0]
                    identifier = (relative.parts[3] if category == 'video'
                                  else relative.parts[3].split('.', 1)[0])
                    identifier = self.identifier(identifier)
                    with self._lock(identifier):
                        self._remove_public_resources(identifier)
                        shutil.rmtree(self._session(identifier), ignore_errors=True)
                        with self.db() as db:
                            db.execute('DELETE FROM ffmpeg_tasks WHERE upload_identifier=?', (identifier,))
                            db.execute('DELETE FROM upload_chunk_cleanup_queue WHERE identifier=?',
                                       (identifier,))
                            db.execute('DELETE FROM uploads WHERE identifier=?', (identifier,))
                    deleted.append(supplied)
                except OSError:
                    failed.append(supplied)
                continue
            match = re.fullmatch(r'/?(?:upload|uploads)/chunks/([A-Za-z0-9_-]{1,128})/?', value)
            if match:
                identifier = self.identifier(match.group(1))
                session = self._session(identifier)
                with self._lock(identifier):
                    if session.is_dir():
                        shutil.rmtree(session)
                        with self.db() as db:
                            db.execute('DELETE FROM ffmpeg_tasks WHERE upload_identifier=?', (identifier,))
                            db.execute('DELETE FROM upload_chunk_cleanup_queue WHERE identifier=?',
                                       (identifier,))
                            db.execute('DELETE FROM uploads WHERE identifier=?', (identifier,))
                        deleted.append(supplied)
                    else:
                        missing.append(supplied)
                continue
            missing.append(supplied)
        return {'deleted': deleted, 'missing': missing, 'failed': failed}

    def delete_many(self, identifiers):
        deleted, missing, failed = [], [], []
        for identifier in dict.fromkeys(identifiers):
            try:
                result = self.delete_identifier(identifier)
                (deleted if result['deleted'] else missing).append(identifier)
            except Exception:
                failed.append(identifier)
        return {'deleted': deleted, 'missing': missing, 'failed': failed}
