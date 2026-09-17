import unittest
from unittest.mock import MagicMock, patch

import requests
from fastapi.testclient import TestClient

import api
from video_data import extract_m3u8, parse_detail, parse_list

URL = 'https://jable.tv/videos/sample-001/'
LIST = '''<div class="video-img-box"><div class="img-box">
<a href="/videos/sample-001/"><img data-src="/cover.jpg"></a>
<span class="label">12:34</span></div><div class="title"><a>Example</a></div></div>
<div class="pagination"><a data-parameters="sort_by:post_date;from:2">Next</a></div>'''
DETAIL = '''<meta property="og:title" content="Example"><meta property="og:image" content="/cover.jpg">
<script type="application/ld+json">{"@type":"VideoObject","uploadDate":"2026-09-09","duration":"PT12M34S"}</script>
<a href="/models/example/">Actor</a><a href="/tags/example/">Tag</a>
<script>var hlsUrl = 'https://cdn.example/video.m3u8?token=abc&amp;x=1';</script>'''


class APITests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app, raise_server_exceptions=False)
        self.key_patch = patch.object(api, 'API_KEY', '')
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)

    def test_list_and_pagination(self):
        with patch('api.fetch_page', return_value=(LIST, 'https://jable.tv/latest-updates/2/')) as fetch:
            response = self.client.get('/api/list?page=2')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['items'][0]['duration'], '12:34')
        fetch.assert_called_once_with('https://jable.tv/latest-updates/2/')
        self.assertTrue(parse_list(LIST, URL, 1)['has_next'])
        self.assertFalse(parse_list(LIST, URL, 2)['has_next'])

    def test_detail(self):
        with patch('api.fetch_page', return_value=(DETAIL, URL)):
            response = self.client.get('/api/detail', params={'url': URL})
        data = response.json()['data']
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data['m3u8_url'], 'https://cdn.example/video.m3u8?token=abc&x=1')
        self.assertEqual(data['actors'], ['Actor'])
        self.assertEqual(data['published_at'], '2026-09-09')

    def test_optional_fields(self):
        data = parse_detail('<script>"https://cdn.example/a.m3u8"</script>', URL)
        self.assertIsNone(data['title'])
        self.assertEqual(data['tags'], [])

    def test_progress_preview_response(self):
        source = DETAIL + r'''<script>var hasVtt = 1; var vttUrl = 'https:\/\/cdn.example/thumbvtt.ts?token=abc\u0026x=1';</script>'''
        with patch('api.fetch_page', return_value=(source, URL)):
            data = self.client.get('/api/detail', params={'url': URL}).json()['data']
        self.assertTrue(data['has_vtt'])
        self.assertEqual(data['vtt_url'], 'https://cdn.example/thumbvtt.ts?token=abc&x=1')

    def test_progress_preview_disabled_missing_and_invalid(self):
        for script, flag, expected in [
            ('var hasVtt = 0; var vttUrl = "https://cdn.example/stale.ts";', False, None),
            ('', None, None),
            ('const hasVtt = true; const vttUrl = "../thumbvtt.ts";', True, 'https://jable.tv/videos/thumbvtt.ts'),
            ('let hasVtt = true; let vttUrl = "javascript:alert(1)";', True, None),
            ('var hasVtt = 1;', True, None),
        ]:
            with self.subTest(script=script):
                data = parse_detail('<script>' + script + '</script>', URL)
                self.assertEqual(data['has_vtt'], flag)
                self.assertEqual(data['vtt_url'], expected)

    def test_visible_publish_date_in_detail_response(self):
        source = DETAIL + '<span class="inactive-color">上市於 <b>2026-09-05</b></span>'
        with patch('api.fetch_page', return_value=(source, URL)):
            response = self.client.get('/api/detail', params={'url': URL})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['published_at'], '2026-09-05')

    def test_invalid_and_unrelated_publish_dates(self):
        for label in ['上市於 2026-02-30', '更新於 2026-09-05']:
            source = DETAIL + f'<span class="inactive-color">{label}</span>'
            self.assertEqual(parse_detail(source, URL)['published_at'], '2026-09-09')
        self.assertIsNone(parse_detail('<span class="inactive-color">暂无日期</span>', URL)['published_at'])

    def test_navigation_and_recommendations_are_not_metadata(self):
        source = '<nav><a href="/tags/unrelated/">Unrelated</a></nav>' + DETAIL
        source += '<div class="video-img-box"><a href="/models/other/">Other</a></div>'
        data = parse_detail(source, URL)
        self.assertEqual(data['tags'], ['Tag'])
        self.assertEqual(data['actors'], ['Actor'])

    def test_zero_padded_page_link(self):
        source = LIST.split('<div class="pagination">')[0] + '<div class="pagination"><a href="#">02</a></div>'
        self.assertTrue(parse_list(source, URL, 1)['has_next'])

    def test_m3u8_escaped_and_multiple_urls(self):
        self.assertEqual(extract_m3u8(r'"https:\/\/cdn.example/a.m3u8?x=1\u0026y=2"; "https://other.example/"'),
                         'https://cdn.example/a.m3u8?x=1&y=2')

    def test_invalid_parameters(self):
        for path in ['/api/list?page=0', '/api/list?page=x', '/api/detail']:
            self.assertEqual(self.client.get(path).status_code, 400)

    def test_untrusted_urls_never_requested(self):
        for url in ['http://jable.tv/videos/a/', 'https://evil.test/videos/a/',
                    'https://jable.tv.evil.test/videos/a/', 'https://jable.tv@127.0.0.1/videos/a/',
                    'https://jable.tv:444/videos/a/', 'https://jable.tv/videos/../admin/',
                    'https://jable.tv/videos/%2e%2e/', 'https://jable.tv/videos/a/?next=http://localhost',
                    'https://jable.tv/latest-updates/']:
            with self.subTest(url=url), patch('api.requests.Session') as session:
                self.assertEqual(self.client.get('/api/detail', params={'url': url}).status_code, 400)
                session.assert_not_called()

    def test_authentication(self):
        with patch.object(api, 'API_KEY', 'test-secret'), patch('api.fetch_page', return_value=(LIST, URL)):
            self.assertEqual(self.client.get('/api/list').status_code, 401)
            self.assertEqual(self.client.get('/api/list', headers={'X-API-Key': 'wrong'}).status_code, 401)
            self.assertEqual(self.client.get('/api/list', headers={'X-API-Key': 'test-secret'}).status_code, 200)

    def test_parse_failure(self):
        with patch('api.fetch_page', return_value=('<html>Challenge</html>', URL)):
            for path in ['/api/list', '/api/detail?url=' + URL]:
                response = self.client.get(path)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()['code'], 50202)

    def mock_response(self, status=200, source=DETAIL, headers=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = status
        response.headers = headers or {}
        response.iter_content.return_value = [source.encode()]
        return response

    def test_upstream_errors(self):
        for upstream, expected in [(404, 404), (403, 502), (500, 502)]:
            with patch('api.requests.Session') as factory:
                factory.return_value.__enter__.return_value.get.return_value = self.mock_response(upstream)
                self.assertEqual(self.client.get('/api/detail', params={'url': URL}).status_code, expected)

    def test_network_timeout(self):
        with patch('api.requests.Session') as factory:
            factory.return_value.__enter__.return_value.get.side_effect = requests.Timeout()
            response = self.client.get('/api/detail', params={'url': URL})
        self.assertEqual(response.status_code, 504)

    def test_cloudflare_challenge(self):
        with patch('api.requests.Session') as factory, patch('api.fetch_browser_page', return_value=(LIST, URL)) as browser:
            factory.return_value.__enter__.return_value.get.return_value = self.mock_response(
                403, headers={'cf-mitigated': 'challenge'})
            response = self.client.get('/api/list')
        self.assertEqual(response.status_code, 200)
        browser.assert_called_once_with('https://jable.tv/latest-updates/', False)

    def test_connection_errors_do_not_expose_secrets(self):
        for error in (requests.exceptions.ProxyError, requests.exceptions.SSLError, requests.ConnectionError):
            with self.subTest(error=error), patch('api.requests.Session') as factory:
                factory.return_value.__enter__.return_value.get.side_effect = error('secret-proxy-password')
                response = self.client.get('/api/list')
                self.assertEqual(response.status_code, 502)
                self.assertNotIn('secret-proxy-password', response.text)

    def test_redirect_validated_before_following(self):
        with patch('api.requests.Session') as factory:
            get = factory.return_value.__enter__.return_value.get
            get.return_value = self.mock_response(302, headers={'Location': 'http://127.0.0.1/'})
            self.assertEqual(self.client.get('/api/detail', params={'url': URL}).status_code, 502)
            self.assertEqual(get.call_count, 1)

    def test_allowed_redirect(self):
        with patch('api.requests.Session') as factory:
            get = factory.return_value.__enter__.return_value.get
            get.side_effect = [self.mock_response(302, headers={'Location': '/videos/sample-002/'}), self.mock_response()]
            source, final = api.fetch_page(URL, detail=True)
        self.assertEqual(final, 'https://jable.tv/videos/sample-002/')
        self.assertIn('hlsUrl', source)

    def test_docs_and_uniform_404(self):
        self.assertEqual(self.client.get('/docs').status_code, 200)
        self.assertIn('/api/detail', self.client.get('/openapi.json').json()['paths'])
        self.assertEqual(self.client.get('/missing').json()['data'], None)


if __name__ == '__main__':
    unittest.main()
