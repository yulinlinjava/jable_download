"""123AV HTML parsing. Never evaluate JavaScript from a source page."""
import json
import re
from datetime import date
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from video_data import absolute, extract_m3u8, text


def parse_list(source, url, page):
    soup = BeautifulSoup(source, 'html.parser')
    items, seen = [], set()
    for card in soup.select('.card'):
        link = card.select_one('a.card__cover[href]')
        href = absolute(url, link.get('href')) if link else None
        if not href or not re.fullmatch(r'/(?:[a-z]{2}/)?v/[a-zA-Z0-9_-]+/?', urlsplit(href).path) or href in seen:
            continue
        seen.add(href)
        img = card.select_one('img.card__img')
        preview = card.select_one('[data-preview]')
        items.append(dict(source='123av', id=urlsplit(href).path.rstrip('/').split('/')[-1],
                          title=text(card.select_one('.card__title')), url=href,
                          cover=absolute(url, (img.get('data-src') or img.get('src')) if img else None),
                          preview_image=absolute(url, preview.get('data-preview')) if preview else None,
                          duration=text(card.select_one('.card__dur'))))
    has_next = False
    current = urlsplit(url)
    for link in soup.select('a[href]'):
        target = urlsplit(absolute(url, link.get('href')) or '')
        if (target.hostname == current.hostname and target.path.rstrip('/') == current.path.rstrip('/')
                and parse_qs(target.query).get('page') == [str(page + 1)]
                and link.get('aria-disabled') != 'true'):
            has_next = True
    return dict(source='123av', page=page, has_next=has_next, items=items)


def parse_detail(source, url):
    soup = BeautifulSoup(source, 'html.parser')
    info = soup.select_one('.watch__info')
    published = None
    if info:
        for row in info.select('.watch__info-row'):
            if text(row.select_one('dt')) == '发布日期':
                try:
                    published = date.fromisoformat(text(row.select_one('dd'))).isoformat()
                except (ValueError, TypeError):
                    pass
    player = soup.select_one('.watch__main .player')
    poster = re.search(r"background-image\s*:\s*url\(['\"]?([^'\")]+)", player.get('style', '')) if player else None
    poster_url = absolute(url, poster.group(1)) if poster else None
    if not poster_url:
        urls = player_urls(source)
        if urls:
            poster_url = absolute(url, parse_qs(urlsplit(urls[0]).query).get('poster', [None])[0])
    preview = player.select_one('[data-preview]') if player else None
    scope = info or BeautifulSoup('', 'html.parser')
    chapters = player_chapters(source)
    return dict(source='123av', id=urlsplit(url).path.rstrip('/').split('/')[-1], url=url,
                title=text(soup.select_one('h1.watch__title')),
                cover=poster_url, poster=poster_url,
                preview_image=absolute(url, preview.get('data-preview')) if preview else None,
                duration=text(scope.select_one('.duration')), published_at=published,
                actors=list(dict.fromkeys(filter(None, [text(a) for a in scope.select('a[href*="/actresses/"]')]))),
                tags=list(dict.fromkeys(filter(None, [text(a) for a in scope.select('a[href*="/genres/"]')]))),
                m3u8_url=extract_m3u8(str(player)) if player else None,
                chapters=chapters)


def player_chapters(source):
    """Return every episode while keeping source-specific player URLs internal."""
    soup = BeautifulSoup(source, 'html.parser')
    node = soup.select_one('.watch__main[x-data]')
    match = re.search(r"\bplayer\(JSON\.parse\('((?:\\.|[^'\\])*)'\)", node.get('x-data', '')) if node else None
    if not match:
        return []
    try:
        decoded = json.loads('"' + match.group(1) + '"')
        episodes = json.loads(decoded)
    except (ValueError, TypeError):
        return []
    if not isinstance(episodes, list):
        return []
    result = []
    for index, item in enumerate(episodes, 1):
        if not isinstance(item, dict) or not isinstance(item.get('url'), str):
            continue
        label = next((item.get(key) for key in ('title', 'name', 'label')
                      if isinstance(item.get(key), str) and item.get(key).strip()), None)
        result.append(dict(index=index, title=label.strip() if label else f'第 {index} 章',
                           duration=item.get('duration') if isinstance(item.get('duration'), str) else None,
                           player_url=item['url']))
    return result


def player_urls(source):
    return [item['player_url'] for item in player_chapters(source)]
