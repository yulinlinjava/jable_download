"""Generate local covers, hover previews, VTT sprites and short HLS playlists."""
import math
import os
import shutil
import subprocess
from pathlib import Path

import m3u8


class MediaPreviewGenerator:
    def __init__(self, timeout=3600, interval=10):
        self.timeout = max(30, int(timeout))
        self.interval = max(1, int(interval))

    def run(self, command, timeout=None):
        try:
            return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=timeout or self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def duration(self, source):
        result = self.run([
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', str(source),
        ], min(self.timeout, 60))
        if not result or result.returncode:
            return None
        try:
            value = float(result.stdout.decode('utf-8', errors='ignore').strip())
            return value if math.isfinite(value) and value > 0 else None
        except ValueError:
            return None

    @staticmethod
    def _valid(path):
        return path.is_file() and path.stat().st_size > 0

    def cover(self, source, directory, duration, need_cover=True, need_poster=True):
        cover, poster = directory / 'cover.jpg', directory / 'poster.jpg'
        if (not need_cover or self._valid(cover)) and (not need_poster or self._valid(poster)):
            return True
        if self._valid(cover):
            if need_poster and not self._valid(poster):
                shutil.copyfile(cover, poster)
            return not need_poster or self._valid(poster)
        seek = min(max((duration or 0) * 0.1, 0), max((duration or 0) - 0.2, 0))
        result = self.run([
            'ffmpeg', '-nostdin', '-y', '-loglevel', 'error',
            '-ss', f'{seek:.3f}', '-i', str(source), '-frames:v', '1',
            '-vf', 'scale=640:360:force_original_aspect_ratio=decrease,'
                   'pad=640:360:(ow-iw)/2:(oh-ih)/2:color=black',
            '-q:v', '3', str(cover),
        ])
        if not result or result.returncode or not self._valid(cover):
            cover.unlink(missing_ok=True)
            return False
        if need_poster and not self._valid(poster):
            shutil.copyfile(cover, poster)
        return (not need_cover or self._valid(cover)) and (not need_poster or self._valid(poster))

    @staticmethod
    def _hover_segments(duration):
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

    def hover(self, source, directory, duration):
        preview = directory / 'preview_image.mp4'
        if self._valid(preview):
            return True
        segments = self._hover_segments(duration)
        command = ['ffmpeg', '-nostdin', '-y', '-loglevel', 'error']
        for start, length in segments:
            command.extend(['-ss', f'{start:.3f}', '-t', f'{length:.3f}', '-i', str(source)])
        filters, labels = [], []
        for index in range(len(segments)):
            label = f'v{index}'
            filters.append(
                f'[{index}:v:0]fps=15,scale=320:180:force_original_aspect_ratio=decrease,'
                f'pad=320:180:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,'
                f'setpts=PTS-STARTPTS[{label}]')
            labels.append(f'[{label}]')
        filters.append(''.join(labels) + f'concat=n={len(labels)}:v=1:a=0[outv]')
        command.extend([
            '-filter_complex', ';'.join(filters), '-map', '[outv]', '-an',
            '-c:v', 'libx264', '-preset', 'superfast', '-crf', '28',
            '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(preview),
        ])
        result = self.run(command)
        if not result or result.returncode or not self._valid(preview):
            preview.unlink(missing_ok=True)
            return False
        return True

    @staticmethod
    def _vtt_time(seconds):
        milliseconds = max(0, round(seconds * 1000))
        hours, milliseconds = divmod(milliseconds, 3600000)
        minutes, milliseconds = divmod(milliseconds, 60000)
        whole_seconds, milliseconds = divmod(milliseconds, 1000)
        return f'{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}'

    def vtt(self, source, directory, duration):
        vtt, first_sheet = directory / 'preview.vtt', directory / 'preview.jpg'
        if self._valid(vtt) and self._valid(first_sheet):
            return True
        if not duration:
            return False
        for path in directory.glob('.preview-sheet-*.jpg'):
            path.unlink(missing_ok=True)
        pattern = directory / '.preview-sheet-%03d.jpg'
        video_filter = (
            f'fps=1/{self.interval},scale=200:112:force_original_aspect_ratio=decrease,'
            'pad=200:112:(ow-iw)/2:(oh-ih)/2:color=black,tile=10x10')
        result = self.run([
            'ffmpeg', '-nostdin', '-y', '-loglevel', 'error', '-i', str(source),
            '-vf', video_filter, '-q:v', '5', '-vsync', 'vfr', str(pattern),
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
        frame_count = min(math.ceil(duration / self.interval), len(names) * 100)
        cues = ['WEBVTT', '']
        for index in range(frame_count):
            start, end = index * self.interval, min((index + 1) * self.interval, duration)
            sheet, cell = names[index // 100], index % 100
            x, y = (cell % 10) * 200, (cell // 10) * 112
            cues.extend([f'{self._vtt_time(start)} --> {self._vtt_time(end)}',
                         f'{sheet}#xywh={x},{y},200,112', ''])
        temporary = vtt.with_suffix('.vtt.part')
        temporary.write_text('\n'.join(cues), encoding='utf-8')
        os.replace(temporary, vtt)
        return frame_count > 0

    @staticmethod
    def _safe_manifest(folder, uri):
        path = (folder / str(uri).split('?', 1)[0]).resolve()
        root = folder.resolve()
        if root not in path.parents or path.suffix.lower() != '.m3u8' or not path.is_file():
            raise ValueError('本地主清单引用了无效的子清单')
        return path

    @staticmethod
    def _trim_playlist(playlist, limit):
        selected, total = [], 0.0
        for segment in playlist.segments:
            duration = max(0.0, float(segment.duration or 0))
            if selected and total + duration > limit:
                break
            selected.append(segment)
            total += duration
            if total >= limit:
                break
        if not selected:
            raise ValueError('本地清单没有可用分片')
        playlist.segments = m3u8.SegmentList(selected)
        playlist.is_endlist = True
        playlist.playlist_type = 'VOD'
        playlist.target_duration = max(1, math.ceil(max(float(item.duration or 0) for item in selected)))
        return playlist

    @staticmethod
    def _write_manifest(path, content):
        temporary = path.with_suffix(path.suffix + '.part')
        temporary.write_text(content, encoding='utf-8')
        os.replace(temporary, path)

    def preview_playlist(self, directory, limit=180, output_name='preview.m3u8'):
        """Create a short playlist without copying or transcoding existing segments."""
        source = directory / 'index.m3u8'
        output = Path(output_name)
        if (not source.is_file() or output.name != output_name
                or output.suffix.lower() != '.m3u8'):
            return False
        track_prefix = output.stem + '-track-'
        try:
            root = m3u8.load(str(source))
            if not root.is_variant:
                self._write_manifest(directory / output_name,
                                     self._trim_playlist(root, limit).dumps())
                return True
            mappings, counter = {}, 0
            references = [item for item in root.playlists]
            references.extend(item for item in root.media if item.uri)
            for item in references:
                original_uri = item.uri
                if original_uri not in mappings:
                    child = self._safe_manifest(directory, original_uri)
                    counter += 1
                    name = f'{track_prefix}{counter:02d}.m3u8'
                    data = m3u8.load(str(child))
                    if data.is_variant:
                        raise ValueError('预览不支持嵌套主清单')
                    self._write_manifest(directory / name,
                                         self._trim_playlist(data, limit).dumps())
                    mappings[original_uri] = name
                item.uri = mappings[original_uri]
            self._write_manifest(directory / output_name, root.dumps())
            return True
        except (OSError, ValueError, TypeError):
            (directory / output_name).unlink(missing_ok=True)
            for path in directory.glob(track_prefix + '*.m3u8'):
                path.unlink(missing_ok=True)
            return False
