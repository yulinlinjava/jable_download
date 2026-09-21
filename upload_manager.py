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


class UploadManager:
    BUFFER_SIZE = 1024 * 1024
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

    def __init__(self, download_root, database=None):
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
                  preview_path TEXT, vtt_path TEXT, source_file TEXT,
                  created REAL NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS uploads_updated
                  ON uploads(updated DESC,identifier);
                CREATE INDEX IF NOT EXISTS uploads_type_status_updated
                  ON uploads(file_type,status,updated DESC);
                CREATE INDEX IF NOT EXISTS uploads_name
                  ON uploads(file_name COLLATE NOCASE);
            ''')
        self.public_base_url = os.getenv('PUBLIC_BASE_URL', 'http://localhost:8000').rstrip('/')
        self.max_chunk_bytes = positive_env('UPLOAD_CHUNK_MAX_BYTES', 50 * 1024 * 1024)
        self.max_file_bytes = positive_env('UPLOAD_FILE_MAX_BYTES', 20 * 1024 * 1024 * 1024)
        self.max_chunks = positive_env('UPLOAD_MAX_CHUNKS', 100000)
        self.hls_time = positive_env('UPLOAD_HLS_TIME', 3)
        self.preview_interval = positive_env('UPLOAD_PREVIEW_INTERVAL', 10)
        self.video_timeout = positive_env('UPLOAD_VIDEO_TIMEOUT', 3600)
        self.merge_workers = positive_env('UPLOAD_MERGE_WORKERS', 1)
        self._guard = threading.Lock()
        self._locks = {}
        self._merge_futures = {}
        self._executor = ThreadPoolExecutor(max_workers=self.merge_workers,
                                            thread_name_prefix='upload-merge')
        # A process restart means no upload is still writing these private files.
        for temporary in self.files_root.glob('.*.part'):
            temporary.unlink(missing_ok=True)
        for temporary in self.chunks_root.glob('*/*/*/.upload-*'):
            temporary.unlink(missing_ok=True)
        with self.db() as db:
            resumable = db.execute("""SELECT * FROM uploads WHERE total_chunks>0 AND status IN
                ('merging','segmenting','generating_cover','generating_preview','generating_vtt')""").fetchall()
        for row in resumable:
            metadata = self._metadata_from_row(row)
            if (metadata.get('fileType') in self.category_roots
                    and metadata.get('status') in {
                    'merging', 'segmenting', 'generating_cover',
                    'generating_preview', 'generating_vtt'}):
                try:
                    self.merge(metadata.get('identifier'), metadata.get('fileName'),
                               int(metadata.get('totalChunks') or 0))
                except Exception:
                    log.exception('Unable to resume upload merge %s', metadata.get('identifier'))

    def close(self):
        self._executor.shutdown(wait=False, cancel_futures=True)

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

    @staticmethod
    def _metadata_from_row(row):
        if not row:
            return {}
        try:
            warnings = json.loads(row['warnings'] or '[]')
        except ValueError:
            warnings = []
        return {
            'identifier': row['identifier'], 'fileName': row['file_name'],
            'fileType': row['file_type'], 'mediaType': row['media_type'],
            'size': row['size'], 'totalChunks': row['total_chunks'],
            'uploadedCount': row['uploaded_count'], 'completed': bool(row['completed']),
            'originalDeleted': bool(row['original_deleted']), 'status': row['status'],
            'processingStage': row['processing_stage'], 'message': row['message'],
            'error': row['error'], 'warnings': warnings, 'path': row['path'],
            'hlsPath': row['hls_path'], 'coverPath': row['cover_path'],
            'posterPath': row['poster_path'], 'previewPath': row['preview_path'],
            'vttPath': row['vtt_path'], 'sourceFile': row['source_file'],
            'createdAt': row['created'], 'updatedAt': row['updated'],
        }

    def _read_metadata(self, session):
        identifier = self.identifier(session.name)
        with self.db() as db:
            return self._metadata_from_row(
                db.execute('SELECT * FROM uploads WHERE identifier=?', (identifier,)).fetchone())

    def _save_metadata(self, data):
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
            data.get('previewPath'), data.get('vttPath'), data.get('sourceFile'),
            data.get('createdAt') or now, data.get('updatedAt') or now,
        )
        with self.db() as db:
            db.execute('''INSERT INTO uploads(identifier,file_name,file_type,media_type,size,
                total_chunks,uploaded_count,completed,original_deleted,status,processing_stage,
                message,error,warnings,path,hls_path,cover_path,poster_path,preview_path,vtt_path,
                source_file,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(identifier) DO UPDATE SET
                file_name=excluded.file_name,file_type=excluded.file_type,
                media_type=excluded.media_type,size=excluded.size,total_chunks=excluded.total_chunks,
                uploaded_count=excluded.uploaded_count,completed=excluded.completed,
                original_deleted=excluded.original_deleted,status=excluded.status,
                processing_stage=excluded.processing_stage,message=excluded.message,error=excluded.error,
                warnings=excluded.warnings,path=excluded.path,hls_path=excluded.hls_path,
                cover_path=excluded.cover_path,poster_path=excluded.poster_path,
                preview_path=excluded.preview_path,vtt_path=excluded.vtt_path,
                source_file=excluded.source_file,updated=excluded.updated''', values)

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
            'path': (metadata or {}).get('path') or self.public_url(relative),
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
                'hlsPath': metadata['hlsPath'],
                'segmented': True,
                'segmentDuration': self.hls_time,
            })
        if metadata:
            for field in ('coverPath', 'posterPath', 'previewPath', 'vttPath'):
                if metadata.get(field):
                    result[field] = metadata[field]
            result['processingStage'] = metadata.get('processingStage') or metadata.get('status')
            result['warnings'] = metadata.get('warnings') or []
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
        return f'{self.public_base_url}/uploads/{path}'

    def _run_media_command(self, command, timeout=None):
        try:
            return subprocess.run(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=timeout or self.video_timeout, check=False)
        except subprocess.TimeoutExpired:
            return None

    def _probe_duration(self, source):
        result = self._run_media_command([
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', str(source),
        ], min(self.video_timeout, 60))
        if not result or result.returncode:
            return None
        try:
            duration = float(result.stdout.decode('utf-8', errors='ignore').strip())
            return duration if math.isfinite(duration) and duration > 0 else None
        except ValueError:
            return None

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

    @staticmethod
    def _vtt_time(seconds):
        milliseconds = max(0, round(seconds * 1000))
        hours, milliseconds = divmod(milliseconds, 3600000)
        minutes, milliseconds = divmod(milliseconds, 60000)
        whole_seconds, milliseconds = divmod(milliseconds, 1000)
        return f'{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}'

    def _generate_cover(self, source, directory, duration):
        seek = min(max((duration or 0) * 0.1, 0), max((duration or 0) - 0.2, 0))
        cover = directory / 'cover.jpg'
        command = [
            'ffmpeg', '-nostdin', '-y', '-loglevel', 'error',
            '-ss', f'{seek:.3f}', '-i', str(source), '-frames:v', '1',
            '-vf', 'scale=640:360:force_original_aspect_ratio=decrease,'
                   'pad=640:360:(ow-iw)/2:(oh-ih)/2:color=black',
            '-q:v', '3', str(cover),
        ]
        result = self._run_media_command(command)
        if not result or result.returncode or not cover.is_file() or cover.stat().st_size == 0:
            cover.unlink(missing_ok=True)
            return False
        shutil.copyfile(cover, directory / 'poster.jpg')
        return True

    def _preview_segments(self, duration):
        if not duration or duration <= 0:
            return [(0.0, 6.0)]
        if duration < 8:
            return [(0.0, max(0.5, min(duration, 6.0)))]
        if duration < 30:
            length = min(1.5, max(0.8, duration / 8))
            return [(min(duration * fraction, max(duration - length, 0)), length)
                    for fraction in (0.15, 0.45, 0.75)]
        return [(min(duration * fraction, duration - 1.5), 1.5)
                for fraction in (0.1, 0.3, 0.5, 0.7, 0.9)]

    def _generate_hover_preview(self, source, directory, duration):
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
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '28',
            '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(preview),
        ])
        result = self._run_media_command(command)
        if not result or result.returncode or not preview.is_file() or preview.stat().st_size == 0:
            preview.unlink(missing_ok=True)
            return False
        return True

    def _generate_vtt_preview(self, source, directory, duration):
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
        ])
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

    def process_video(self, source, identifier):
        identifier = self.identifier(identifier)
        target_dir = self._video_directory(identifier)
        temporary = self.files_root / ('.video-' + uuid.uuid4().hex)
        temporary.mkdir()
        warnings = []
        try:
            duration = self._probe_duration(source)
            self._set_stage(identifier, 'segmenting', '正在生成 HLS 视频切片')
            command = [
                'ffmpeg', '-nostdin', '-y', '-loglevel', 'error', '-i', str(source),
                '-map', '0:v:0?', '-map', '0:a:0?', '-c', 'copy',
                '-hls_time', str(self.hls_time), '-hls_playlist_type', 'vod',
                '-hls_flags', 'split_by_time',
                '-hls_segment_filename', str(temporary / '%04d.ts'),
                str(temporary / 'index.m3u8'),
            ]
            result = self._run_media_command(command)
            playlist = temporary / 'index.m3u8'
            segments = list(temporary.glob('[0-9][0-9][0-9][0-9].ts'))
            if not result:
                raise UploadError(504, 50410, '视频切片超时')
            if result.returncode or not playlist.is_file() or not segments:
                raise UploadError(422, 42210, '视频切片失败，请检查视频编码或文件完整性')

            self._set_stage(identifier, 'generating_cover', '正在生成视频封面')
            if not self._generate_cover(source, temporary, duration):
                warnings.append('视频封面生成失败')

            self._set_stage(identifier, 'generating_preview', '正在生成列表悬浮预览视频')
            if not self._generate_hover_preview(source, temporary, duration):
                warnings.append('列表悬浮预览视频生成失败')

            self._set_stage(identifier, 'generating_vtt', '正在生成播放器进度预览')
            if not duration:
                warnings.append('无法读取视频时长，未生成播放器进度预览')
            elif not self._generate_vtt_preview(source, temporary, duration):
                warnings.append('播放器进度预览生成失败')

            target_dir.parent.mkdir(parents=True, exist_ok=True)
            if target_dir.exists():
                shutil.rmtree(target_dir)
            os.replace(temporary, target_dir)
            relative = target_dir.relative_to(self.files_root).as_posix()
            result = {
                'hlsPath': self.public_url(f'{relative}/index.m3u8'),
                'warnings': warnings,
            }
            for field, name in (
                    ('coverPath', 'cover.jpg'), ('posterPath', 'poster.jpg'),
                    ('previewPath', 'preview_image.mp4'), ('vttPath', 'preview.vtt')):
                if (target_dir / name).is_file():
                    result[field] = self.public_url(f'{relative}/{name}')
            return result
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def process_preview(self, target, identifier):
        if self.preview_type(target.name) == 'video':
            return self.process_video(target, identifier)
        return {}

    def _available_target(self, name, identifier):
        name = self.filename(name)
        category = self.file_type(name)
        if category == 'video':
            # The original video is private and is deleted after derived files are ready.
            return self.files_root / f'.source-{identifier}-{uuid.uuid4().hex[:8]}{Path(name).suffix}'
        target = self._single_file_target(name, identifier)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def upload_file(self, upload):
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
                result = self._result(completed, existing.get('size') or size,
                                      identifier, existing)
                result['deduplicated'] = True
                return result
            if existing.get('status') in {
                    'merging', 'segmenting', 'generating_cover',
                    'generating_preview', 'generating_vtt'}:
                temporary.unlink(missing_ok=True)
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
                self._save_metadata({
                    'identifier': identifier, 'fileName': name, 'fileType': self.file_type(name),
                    'mediaType': mimetypes.guess_type(name)[0] or 'application/octet-stream',
                    'size': size, 'totalChunks': 0, 'uploadedCount': 1,
                    'completed': False, 'status': 'merging', 'processingStage': 'merging',
                    'message': '正在处理上传文件', 'warnings': [],
                    'createdAt': now, 'updatedAt': now,
                })
                preview = self.process_preview(target, identifier)
                original_deleted = bool(preview.get('hlsPath'))
                if original_deleted:
                    target.unlink()
                    completed_target = self.resolve_file(preview['hlsPath'])
                else:
                    completed_target = target
                metadata = {
                    'identifier': identifier, 'fileName': name, 'totalChunks': 1,
                    'uploadedCount': 1,
                    'completed': True,
                    'path': preview.get('hlsPath') or self.public_url(
                        target.relative_to(self.files_root).as_posix()),
                    'size': size, 'hlsPath': preview.get('hlsPath'),
                    'fileType': self.file_type(name),
                    'mediaType': mimetypes.guess_type(name)[0] or 'application/octet-stream',
                    'originalDeleted': original_deleted,
                    'status': 'completed', 'processingStage': 'completed', 'error': None,
                    'message': '上传完成', 'createdAt': now, 'updatedAt': now,
                }
                metadata.update(preview)
                self._save_metadata(metadata)
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                if isinstance(locals().get('target'), Path):
                    target.unlink(missing_ok=True)
                self._remove_public_resources(identifier)
                shutil.rmtree(self._session(identifier), ignore_errors=True)
                with self.db() as db:
                    db.execute('DELETE FROM uploads WHERE identifier=?', (identifier,))
                if isinstance(exc, OSError):
                    raise UploadError(507, 50710, '上传存储目录不可用或磁盘写入失败') from exc
                raise
            result = self._result(completed_target, size, identifier, metadata)
            result['deduplicated'] = False
            return result

    def check_chunks(self, identifier):
        identifier = self.identifier(identifier)
        session = self._session(identifier)
        with self._lock(identifier):
            metadata = self._read_metadata(session)
            chunks = self._chunk_numbers(session)
            total = metadata.get('totalChunks')
            completed_path = metadata.get('path')
            completed_file = self.resolve_file(completed_path) if metadata.get('completed') and completed_path else None
            completed = bool(completed_file)
            result = {
                'identifier': identifier,
                'uploadedChunks': chunks,
                'uploadedCount': len(chunks),
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
                'updatedAt': metadata.get('updatedAt'),
            }
            for field in ('coverPath', 'posterPath', 'previewPath', 'vttPath'):
                result[field] = metadata.get(field)
            if completed_file:
                result.update(self._result(completed_file, metadata.get('size') or completed_file.stat().st_size,
                                           identifier, metadata))
            return result

    def upload_chunk(self, identifier, chunk_number, total_chunks, upload):
        identifier = self.identifier(identifier)
        upload_name = self.safe_filename(upload.filename or '')
        if chunk_number < 1 or chunk_number > self.max_chunks:
            raise UploadError(400, 40013, 'chunkNumber 超出允许范围')
        if total_chunks is not None and (total_chunks < 1 or total_chunks > self.max_chunks):
            raise UploadError(400, 40014, 'totalChunks 超出允许范围')
        if total_chunks is not None and chunk_number > total_chunks:
            raise UploadError(400, 40015, 'chunkNumber 不能大于 totalChunks')
        session = self._session(identifier)
        session.mkdir(parents=True, exist_ok=True)
        temporary = session / ('.upload-' + uuid.uuid4().hex)
        size = self._write_upload(upload, temporary, self.max_chunk_bytes)
        chunk_path = session / f'{chunk_number:06d}.part'
        now = time.time()
        with self._lock(identifier):
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
            os.replace(temporary, chunk_path)
            metadata.update({
                'identifier': identifier,
                'fileName': metadata.get('fileName') or upload_name,
                'fileType': metadata.get('fileType') or self.file_type(upload_name),
                'mediaType': metadata.get('mediaType') or mimetypes.guess_type(upload_name)[0]
                             or 'application/octet-stream',
                'totalChunks': total_chunks or previous_total,
                'uploadedCount': len(self._chunk_numbers(session)),
                'completed': False,
                'status': 'uploading',
                'error': None,
                'message': f'已上传分片 {chunk_number}',
                'createdAt': metadata.get('createdAt') or now,
                'updatedAt': now,
            })
            self._save_metadata(metadata)
            uploaded = self._chunk_numbers(session)
        return {
            'identifier': identifier,
            'chunkNumber': chunk_number,
            'totalChunks': metadata.get('totalChunks'),
            'size': size,
            'uploadedCount': len(uploaded),
        }

    def merge(self, identifier, file_name, total_chunks):
        identifier = self.identifier(identifier)
        file_name = self.safe_filename(file_name)
        if total_chunks < 1 or total_chunks > self.max_chunks:
            raise UploadError(400, 40014, 'totalChunks 超出允许范围')
        session = self._session(identifier)
        with self._lock(identifier):
            metadata = self._read_metadata(session)
            existing_total = metadata.get('totalChunks')
            if existing_total and existing_total != total_chunks:
                raise UploadError(409, 40910, 'totalChunks 与已有上传记录不一致')
            if metadata.get('completed'):
                completed = self.resolve_file(metadata.get('path'))
                if completed:
                    return self._result(completed, metadata.get('size') or completed.stat().st_size,
                                        identifier, metadata)
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
            predicted = ({
                'coverPath': self.public_url(f'{video_relative}/cover.jpg'),
                'posterPath': self.public_url(f'{video_relative}/poster.jpg'),
                'previewPath': self.public_url(f'{video_relative}/preview_image.mp4'),
                'vttPath': self.public_url(f'{video_relative}/preview.vtt'),
            } if video else {})
            metadata.update({
                'identifier': identifier, 'fileName': file_name, 'totalChunks': total_chunks,
                'fileType': file_type,
                'status': 'merging', 'processingStage': 'merging', 'completed': False,
                'path': primary_path, 'hlsPath': hls_path, 'error': None,
                'warnings': [],
                'message': '正在后台合并和切片' if video else '正在后台合并文件',
                'createdAt': metadata.get('createdAt') or now, 'updatedAt': now,
            })
            metadata.update(predicted)
            self._save_metadata(metadata)
            with self._guard:
                future = self._merge_futures.get(identifier)
                if not future or future.done():
                    future = self._executor.submit(
                        self._run_merge, identifier, file_name, total_chunks)
                    self._merge_futures[identifier] = future
                    future.add_done_callback(lambda _future, current=identifier: self._merge_done(current))
            response = {
                'identifier': identifier, 'fileName': file_name, 'status': 'merging',
                'processingStage': 'merging', 'completed': False,
                'path': primary_path, 'hlsPath': hls_path,
                'fileType': file_type,
                'previewType': 'video' if video else self.preview_type(file_name),
                'message': metadata['message'], 'warnings': [],
            }
            response.update(predicted)
            return response

    def _merge_done(self, identifier):
        with self._guard:
            self._merge_futures.pop(identifier, None)

    def _run_merge(self, identifier, file_name, total_chunks):
        try:
            self._merge(identifier, file_name, total_chunks)
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

    def _merge(self, identifier, file_name, total_chunks):
        identifier = self.identifier(identifier)
        file_name = self.safe_filename(file_name)
        if total_chunks < 1 or total_chunks > self.max_chunks:
            raise UploadError(400, 40014, 'totalChunks 超出允许范围')
        session = self._session(identifier)
        with self._lock(identifier):
            metadata = self._read_metadata(session)
            existing_total = metadata.get('totalChunks')
            if existing_total and existing_total != total_chunks:
                raise UploadError(409, 40910, 'totalChunks 与已有上传记录不一致')
            completed = metadata.get('path') if metadata.get('completed') else None
            if completed:
                target = self.resolve_file(completed)
                if target:
                    return self._result(target, target.stat().st_size,
                                        identifier, metadata)
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
            return self._result(completed_target, size, identifier, metadata)

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
            escaped = keyword.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            conditions.append("file_name LIKE ? ESCAPE '\\'")
            values.append(f'%{escaped}%')
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
                              + ' ORDER BY updated DESC,identifier DESC LIMIT ? OFFSET ?',
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
            self._merge_futures.pop(identifier, None)
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
