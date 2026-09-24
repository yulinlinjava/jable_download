"""Validate pasted resource metadata without accepting internal task fields."""
import re
from datetime import date
from urllib.parse import urlsplit


def identifier(value, field):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value)
            or value.upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)),
                                 *(f'LPT{i}' for i in range(10))}):
        raise ValueError(f'{field} 只能包含 1–100 位英文字母、数字、下划线或连字符，且不能为系统保留名称')
    return value


def resource_url(value, field):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) > 8192 or re.search(r'[\s\\\x00-\x1f]', value):
            raise ValueError()
        parts = urlsplit(value)
        relative_local = not parts.scheme and not parts.netloc and parts.path.startswith('/')
        absolute_http = (parts.scheme in ('http', 'https') and bool(parts.hostname)
                         and not parts.username and not parts.password)
        if not (relative_local or absolute_http) or parts.fragment:
            raise ValueError()
        parts.port
    except (ValueError, TypeError):
        raise ValueError(f'{field} 必须是 HTTP/HTTPS 地址或以 / 开头的本地公开路径') from None
    return value


def validate_resource(value):
    if not isinstance(value, dict):
        raise ValueError('每条资源必须是 JSON 对象')
    result = {key: identifier(value.get(key), key) for key in ('source', 'id')}
    result['source'] = result['source'].lower()
    title = value.get('title')
    if not isinstance(title, str) or not title.strip() or len(title) > 1000:
        raise ValueError('title 必须是 1–1000 字符的非空字符串')
    result['title'] = title.strip()
    for field in ('url', 'video_url', 'cover', 'poster', 'preview_image', 'm3u8_url',
                  'vtt_url', 'preview_m3u8_url', 'preview5_m3u8_url'):
        result[field] = resource_url(value.get(field), field)
    raw_chapters = value.get('chapters')
    first_chapter_url = (raw_chapters[0].get('m3u8_url')
                         if isinstance(raw_chapters, list) and raw_chapters
                         and isinstance(raw_chapters[0], dict) else None)
    direct_url = result['m3u8_url'] or result['video_url'] or (
        resource_url(first_chapter_url, 'chapters[1].m3u8_url') if first_chapter_url else None)
    if not direct_url and result['url'] and urlsplit(result['url']).path.lower().endswith('.m3u8'):
        direct_url = result['url']
    result['m3u8_url'] = direct_url
    if not result['m3u8_url']:
        raise ValueError('m3u8_url 或 video_url 必填，请提供视频播放地址')
    for field in ('actors', 'tags'):
        items = value.get(field, [])
        if not isinstance(items, list) or len(items) > 100 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 200 for item in items):
            raise ValueError(f'{field} 必须是字符串数组，最多 100 项')
        result[field] = list(dict.fromkeys(item.strip() for item in items))
    duration = value.get('duration')
    if duration is not None and (not isinstance(duration, str) or len(duration) > 50):
        raise ValueError('duration 必须是字符串或 null')
    result['duration'] = duration
    published = value.get('published_at')
    if published is not None:
        try:
            if not isinstance(published, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', published):
                raise ValueError()
            date.fromisoformat(published)
        except ValueError:
            raise ValueError('published_at 必须是有效的 YYYY-MM-DD 日期或 null') from None
    result['published_at'] = published
    chapters = value.get('chapters', [])
    if chapters is None:
        chapters = []
    if not isinstance(chapters, list) or len(chapters) > 100:
        raise ValueError('chapters 必须是数组，最多 100 项')
    normalized = []
    for index, chapter in enumerate(chapters, 1):
        if not isinstance(chapter, dict):
            raise ValueError('chapters 中的每一项必须是对象')
        stream = resource_url(chapter.get('m3u8_url'), f'chapters[{index}].m3u8_url')
        if not stream:
            raise ValueError(f'chapters[{index}].m3u8_url 必填')
        label = chapter.get('title')
        if label is not None and (not isinstance(label, str) or len(label.strip()) > 200):
            raise ValueError(f'chapters[{index}].title 格式无效')
        normalized.append(dict(index=index, title=label.strip() if label and label.strip() else f'第 {index} 章',
                               duration=chapter.get('duration') if isinstance(chapter.get('duration'), str) else None,
                               m3u8_url=stream,
                               vtt_url=resource_url(chapter.get('vtt_url'), f'chapters[{index}].vtt_url'),
                               preview_m3u8_url=resource_url(
                                   chapter.get('preview_m3u8_url'),
                                   f'chapters[{index}].preview_m3u8_url'),
                               preview5_m3u8_url=resource_url(
                                   chapter.get('preview5_m3u8_url'),
                                   f'chapters[{index}].preview5_m3u8_url')))
    result['chapters'] = normalized
    if normalized:
        result['m3u8_url'] = normalized[0]['m3u8_url']
    # Only server-controlled imports may opt out of the page resolver.
    result['_imported'] = True
    return result
