import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import api
import provider_123av as parser

URL = 'https://123av.com/cn/v/sample-001'
LIST_URL = 'https://123av.com/cn/makers/fc2?sort=today&page=2'
LIST = '''<div class="card"><a class="card__cover" href="/cn/v/sample-001"><img class="card__img" src="/cover.jpg"></a>
<h3 class="card__title">Example</h3><span class="card__dur">12:34</span></div>
<a href="?sort=today&amp;page=3">Next</a>'''
DETAIL = '''<div class="watch__main"><div class="player" style="background-image:url('https://cdn.example/cover.jpg')"></div></div>
<h1 class="watch__title">Example</h1><dl class="watch__info"><div class="watch__info-row"><dt>发布日期</dt><dd>2026-09-10</dd></div>
<a href="/cn/genres/example">Tag</a></dl><footer><a href="/cn/actresses/other">Unrelated</a></footer>'''


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app, raise_server_exceptions=False)
        p = patch.object(api, 'API_KEY', '')
        p.start()
        self.addCleanup(p.stop)

    def test_list_url_overrides_page(self):
        with patch('api.fetch_page', return_value=(LIST, LIST_URL)) as fetch:
            response = self.client.get('/api/list', params={'url': LIST_URL, 'page': 9})
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertEqual(data['page'], 2)
        self.assertEqual(data['source'], '123av')
        self.assertTrue(data['has_next'])
        self.assertEqual(data['items'][0]['url'], URL)
        fetch.assert_called_once_with(LIST_URL)

    def test_metadata_scope(self):
        data = parser.parse_detail(DETAIL, URL)
        self.assertEqual(data['published_at'], '2026-09-10')
        self.assertEqual(data['cover'], 'https://cdn.example/cover.jpg')
        self.assertEqual(data['actors'], [])
        self.assertEqual(data['tags'], ['Tag'])
        self.assertEqual(data['poster'], 'https://cdn.example/cover.jpg')

    def test_preview_images_in_list_response(self):
        source = LIST.replace('<div class="card">', '<div class="card"><div data-preview="/preview.png?token=abc&amp;x=1"></div>')
        with patch('api.fetch_page', return_value=(source, LIST_URL)):
            response = self.client.get('/api/list', params={'url': LIST_URL})
        item = response.json()['data']['items'][0]
        self.assertEqual(item['preview_image'], 'https://123av.com/preview.png?token=abc&x=1')
        self.assertEqual(item['cover'], 'https://123av.com/cover.jpg')

    def test_missing_preview_not_guessed(self):
        self.assertIsNone(parser.parse_list(LIST, LIST_URL, 2)['items'][0]['preview_image'])
        self.assertIsNone(parser.parse_detail('', URL)['poster'])

    def test_episode_json_without_eval(self):
        episodes = json.dumps([{'url': 'https://javplayer.cc/e/ABC123'}])
        escaped = json.dumps(episodes)[1:-1].replace(r'\"', r'\u0022')
        source = '<div class="watch__main" x-data="player(JSON.parse(\'' + escaped + '\'), 1)"></div>'
        self.assertEqual(parser.player_urls(source), ['https://javplayer.cc/e/ABC123'])
        self.assertEqual(parser.player_urls('<div class="watch__main" x-data="alert(1)"></div>'), [])

    def test_detail_resolves_player(self):
        with patch('api.fetch_page', return_value=(DETAIL, URL)), patch('provider_123av.player_urls', return_value=['https://javplayer.cc/e/ABC123']), patch('api.resolve_player', return_value='https://cdn.example/video.m3u8'):
            response = self.client.get('/api/detail', params={'url': URL})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['source'], '123av')
        self.assertEqual(response.json()['data']['poster'], 'https://cdn.example/cover.jpg')

    def test_reject_bad_list_urls_before_network(self):
        for url in [LIST_URL + '&page=3', LIST_URL + '&redirect=http://localhost',
                    LIST_URL.replace('page=2', 'page=0'), LIST_URL.replace('today', 'unknown'),
                    LIST_URL.replace('123av.com', '123av.com.evil.test'), URL]:
            with self.subTest(url=url), patch('api.fetch_page') as fetch:
                self.assertEqual(self.client.get('/api/list', params={'url': url}).status_code, 400)
                fetch.assert_not_called()

    def test_player_whitelist_before_browser(self):
        with self.assertRaises(api.APIError):
            api.resolve_player('https://127.0.0.1/e/ABC')

    def test_jable_link_pagination(self):
        with patch('api.fetch_page', return_value=('<div class="video-img-box"><div class="img-box"><a href="/videos/test/"></a></div></div>', 'https://jable.tv/latest-updates/3/')):
            response = self.client.get('/api/list', params={'url': 'https://jable.tv/latest-updates/3/'})
        self.assertEqual(response.json()['data']['page'], 3)
        self.assertEqual(response.json()['data']['source'], 'jable')
