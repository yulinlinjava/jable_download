"""Side-effect-free page parsers shared by the API and downloader."""
import html
import json
import re
from datetime import date
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


def extract_m3u8(source):
    source = html.unescape(source).replace(r'\/', '/').replace(r'\u0026', '&')
    match = re.search(r'https?://[^\s\x22\x27<>\\]+?\.m3u8(?:\?[^\s\x22\x27<>\\]*)?', source)
    return match.group(0) if match else None


def text(node):
    return (node.get_text(' ', strip=True) or None) if node else None


def model_name(link):
    """Read a Jable model name from text or its tooltip-only avatar."""
    if not link:
        return None
    # Jable sometimes renders only a shortened visible name (for example
    # "泉") while keeping the complete model name on an avatar or placeholder
    # tooltip ("泉ももか"). The tooltip is therefore the authoritative value.
    tooltip = link if link.get('data-original-title') else link.select_one('[data-original-title]')
    if tooltip:
        value = (tooltip.get('data-original-title') or '').strip()
        if value:
            return value
    name = text(link)
    if name:
        return name
    for node in (link, link.select_one('img, span')):
        if not node:
            continue
        for attribute in ('title', 'alt'):
            value = (node.get(attribute) or '').strip()
            if value:
                return value
    return None


def localized_model_names(title, links):
    """Prefer the localized model names appended to a Jable video title."""
    resolved = [(link, model_name(link)) for link in links]
    resolved = [(link, name) for link, name in resolved if name]
    fallback = list(dict.fromkeys(name for _, name in resolved))
    if not title or not fallback:
        return fallback
    clean_title = re.sub(r'\s*-\s*Jable(?:\.TV)?.*$', '', title, flags=re.I).strip()
    parts = [part.strip(' ,，、') for part in re.split(r'\s+', clean_title) if part.strip(' ,，、')]
    count = len(fallback)
    if len(parts) < count:
        return fallback
    candidates = parts[-count:]
    if any(not value or len(value) > 40 or '://' in value for value in candidates):
        return fallback
    for candidate, (link, fallback_name) in zip(candidates, resolved):
        visible_name = text(link)
        clue = visible_name or fallback_name
        if clue and candidate != clue and clue not in candidate and candidate not in clue:
            return fallback
    return list(dict.fromkeys(candidates))


def meta(soup, key):
    node = soup.find('meta', attrs={'property': key}) or soup.find('meta', attrs={'name': key})
    return (node.get('content') or None) if node else None


def absolute(base, value):
    if not value:
        return None
    result = urljoin(base, value)
    return result if urlsplit(result).scheme in ('https', 'http') else None


def visible_publish_date(soup):
    for node in soup.select('.inactive-color'):
        match = re.search(r'上市[於于]\s*[:：]?\s*(\d{4}-\d{2}-\d{2})(?!\d)', text(node) or '')
        if match:
            try:
                return date.fromisoformat(match.group(1)).isoformat()
            except ValueError:
                continue
    return None


def progress_preview(soup, url):
    """Read literal player variables without executing source JavaScript."""
    for script in soup.select('script:not([src])'):
        content = script.get_text()
        flag = re.search(r'\b(?:var|let|const)\s+hasVtt\s*=\s*(1|0|true|false)\s*;', content)
        if not flag:
            continue
        if flag.group(1) in ('0', 'false'):
            return False, None
        match = re.search(r'''\b(?:var|let|const)\s+vttUrl\s*=\s*(['"])((?:\\.|[^\\])*?)\1\s*;''', content)
        if not match:
            return True, None
        raw = match.group(2)
        # Decode only JSON string escapes, never eval arbitrary script content.
        try:
            decoded = json.loads('"' + raw.replace("\\'", "'").replace('"', '\\"') + '"')
        except ValueError:
            return True, None
        return True, absolute(url, html.unescape(decoded))
    return None, None


def parse_list(source, url, page):
    soup = BeautifulSoup(source, 'html.parser')
    items, seen = [], set()
    for card in soup.select('.video-img-box'):
        link = card.select_one('.img-box a[href*="/videos/"]')
        if not link:
            continue
        href = absolute(url, link.get('href'))
        if not href or href in seen:
            continue
        seen.add(href)
        img = card.select_one('img')
        preview = card.select_one('[data-preview]')
        items.append(dict(
            id=urlsplit(href).path.rstrip('/').split('/')[-1],
            title=text(card.select_one('.title a, .title')) or (img.get('alt') if img else None),
            url=href,
            cover=absolute(url, (img.get('data-src') or img.get('src')) if img else None),
            preview_image=absolute(url, preview.get('data-preview')) if preview else None,
            duration=text(card.select_one('.label, .duration')),
        ))
    has_next = False
    for link in soup.select('.pagination a, a[rel="next"]'):
        parent_classes = link.parent.get('class', [])
        if 'disabled' in parent_classes or link.get('aria-disabled') == 'true':
            continue
        href = link.get('href', '')
        params = link.get('data-parameters', '')
        if ('next' in link.get('rel', []) or
                re.search(r'(?:from|from_videos)[:=]' + str(page + 1) + r'(?:[;&]|$)', params) or
                re.search(r'/latest-updates/' + str(page + 1) + r'/?(?:\?|$)', href) or
                (text(link) or '').lstrip('0') == str(page + 1)):
            has_next = True
    return dict(page=page, has_next=has_next, items=items)


def parse_detail(source, url):
    soup = BeautifulSoup(source, 'html.parser')
    # Navigation tags and recommended cards are not metadata for this video.
    for node in soup.select('header, nav, aside, footer, .video-img-box'):
        node.decompose()
    structured = {}
    def visit(value):
        nonlocal structured
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            kind = value.get('@type')
            if kind == 'VideoObject' or (isinstance(kind, list) and 'VideoObject' in kind):
                structured = value
            for key in ('@graph', 'mainEntity'):
                if key in value:
                    visit(value[key])
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            pass
    thumb = structured.get('thumbnailUrl')
    if isinstance(thumb, list):
        thumb = thumb[0] if thumb else None
    time_node = soup.select_one('time[datetime]')
    video = soup.select_one('video[poster]')
    heading_title = text(soup.select_one('h1, h4.video-title, h4.title'))
    detail_title = meta(soup, 'og:title') or heading_title or structured.get('name')
    model_links = soup.select('a[href*="/models/"]')
    has_vtt, vtt_url = progress_preview(soup, url)
    return dict(
        id=urlsplit(url).path.rstrip('/').split('/')[-1],
        title=detail_title,
        url=url,
        cover=absolute(url, meta(soup, 'og:image') or thumb),
        poster=absolute(url, video.get('poster')) if video else None,
        preview_image=None,
        has_vtt=has_vtt,
        vtt_url=vtt_url,
        duration=meta(soup, 'video:duration') or structured.get('duration') or text(soup.select_one('.video-info .duration')),
        published_at=visible_publish_date(soup) or structured.get('uploadDate') or meta(soup, 'article:published_time') or (time_node.get('datetime') if time_node else None),
        actors=localized_model_names(heading_title or detail_title, model_links),
        tags=list(dict.fromkeys(filter(None, [
            text(a) for a in soup.select('a[href*="/categories/"], a[href*="/tags/"]')
        ]))),
        m3u8_url=extract_m3u8(source),
    )
