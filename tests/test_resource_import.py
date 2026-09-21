import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import api
from collection_tasks import CollectionManager
from download_tasks import DownloadError, DownloadManager, HttpTransport
from resource_import import validate_resource


def resource(**changes):
    return dict(source='123av', id='demo-001', title='Example',
                m3u8_url='https://cdn.example.com/demo/index.m3u8',
                actors=['Actor'], tags=['Tag'], published_at='2026-09-18', **changes)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.downloads = DownloadManager(self.temp.name, start_enabled=False)
        self.addCleanup(self.downloads.close)
        # No collection scheduler is needed in these deterministic tests.
        self.collections = CollectionManager.__new__(CollectionManager)
        self.collections.database = self.downloads.database
        self.collections.downloads = self.downloads
        self.collections.scheduler_wake = threading.Event()
        self.client = TestClient(api.app)
        for name, value in [('collections', self.collections), ('downloads', self.downloads)]:
            original = getattr(api.app.state, name, None)
            setattr(api.app.state, name, value)
            self.addCleanup(setattr, api.app.state, name, original)
        key = patch.object(api, 'API_KEY', '')
        key.start(); self.addCleanup(key.stop)

    def test_import_list_metadata_and_manual_schedule(self):
        response = self.client.post('/api/collection-items/import', json=[resource()])
        self.assertEqual(response.status_code, 200)
        result = response.json()['data']
        self.assertEqual((result['created'], result['queued']), (1, 0))
        task_id = result['results'][0]['task_id']
        with self.downloads.db() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(json.loads(row['metadata_json'])['actors'], ['Actor'])
        listed = self.client.get('/api/collection-items').json()['data']['items']
        self.assertEqual(listed[0]['title'], 'Example')
        self.assertEqual(self.client.post(f'/api/collection-items/{task_id}/download').status_code, 200)
        self.assertEqual(self.downloads.get(task_id)['status'], 'queued')

    def test_partial_failure_duplicate_and_repeated_request(self):
        other = resource(); other.update(id='demo-002', title='Second', source='external')
        duplicate = resource(); duplicate.update(id='DEMO-001', title='Different')
        same_title = resource(); same_title.update(id='demo-003', title=' EXAMPLE ')
        result = self.collections.import_items([resource(), duplicate, same_title, None,
                                                dict(other, m3u8_url=None), other], True)
        self.assertEqual((result['created'], result['skipped'], result['failed'], result['queued']), (2, 2, 2, 2))
        self.assertEqual(self.collections.import_items([resource()])['skipped'], 1)
        self.assertEqual(self.client.get('/api/collection-items?source=external').json()['data']['total'], 1)

    def test_invalid_shape_and_auth(self):
        for value in ({}, [], [resource()] * 1001):
            self.assertEqual(self.client.post('/api/collection-items/import', json=value).status_code, 400)
        self.assertEqual(self.client.post('/api/collection-items/import', content='[bad',
                         headers={'Content-Type': 'application/json'}).status_code, 400)
        with patch.object(api, 'API_KEY', 'secret'):
            self.assertEqual(self.client.post('/api/collection-items/import', json=[resource()]).status_code, 401)

    def test_immediate_queue_via_api(self):
        response = self.client.post('/api/collection-items/import?download=true', json=[resource()])
        self.assertEqual(response.status_code, 200)
        result = response.json()['data']
        self.assertEqual(result['queued'], 1)
        self.assertEqual(self.downloads.get(result['results'][0]['task_id'])['status'], 'queued')

    def test_validation_and_internal_fields(self):
        for field, value in [('id', '../escape'), ('source', 'CON'), ('actors', 'Actor'),
                             ('published_at', '2026-02-30'), ('title', ' '),
                             ('m3u8_url', 'http://127.0.0.1/a.m3u8'), ('cover', 'file:///x')]:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    validate_resource(dict(resource(), **{field: value}))
        clean = validate_resource(dict(resource(), m3u8_local='../secret', status='completed'))
        self.assertNotIn('m3u8_local', clean)
        self.assertNotIn('status', clean)

    def test_imported_hls_download_to_disk_without_page_resolver(self):
        item = resource(); item.update(source='external', cover='https://cdn.example.com/cover.jpg')
        task_id = self.collections.import_items([item])['results'][0]['task_id']
        with self.downloads.db() as db:
            task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
        segment = (b'G' + bytes(187)) * 2
        playlist = b'#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\npart.ts\n#EXT-X-ENDLIST\n'
        def fetch(url, **kwargs):
            return playlist if url.endswith('.m3u8') else b'\xff\xd8\xffimage' if url.endswith('.jpg') else segment
        with patch.object(HttpTransport, 'get', side_effect=fetch), patch.object(self.downloads, 'resolver') as resolver:
            self.downloads.download(task)
            resolver.assert_not_called()
        completed = self.downloads.get(task_id)
        self.assertEqual(completed['status'], 'completed')
        folder = Path(completed['save_path'])
        self.assertEqual((folder / '0000.ts').read_bytes(), segment)
        self.assertTrue((folder / 'index.m3u8').is_file())
        self.assertIn('/external/external/demo-001/', completed['m3u8_url'])
        with self.downloads.db() as db:
            metadata = json.loads(db.execute('SELECT metadata_json FROM tasks WHERE id=?', (task_id,)).fetchone()[0])
        self.assertEqual(metadata['actors'], ['Actor'])
        self.assertTrue(metadata['_imported'])


class TransportTests(unittest.TestCase):
    def response(self, status=200, data=b'abc', headers=None):
        response = MagicMock(status_code=status, headers=headers or {})
        response.__enter__.return_value = response
        response.iter_content.return_value = [data]
        return response

    def test_range_and_size_validation(self):
        session = MagicMock()
        transport = HttpTransport(session)
        with patch('download_tasks.public_url'):
            session.get.return_value = self.response(200)
            with self.assertRaises(DownloadError):
                transport.get('https://cdn.example/a', byte_range=[0, 3])
            session.get.return_value = self.response(206)
            self.assertEqual(transport.get('https://cdn.example/a', byte_range=[0, 3]), b'abc')
            self.assertEqual(session.get.call_args.kwargs['headers']['Range'], 'bytes=0-2')
            session.get.return_value = self.response(200)
            with self.assertRaises(DownloadError):
                transport.get('https://cdn.example/a', limit=2)

    def test_redirect_cannot_fetch_private_address(self):
        session = MagicMock()
        session.get.return_value = self.response(302, headers={'Location': 'http://127.0.0.1/private'})
        with patch('download_tasks.public_url', side_effect=[None, DownloadError('private')]):
            with self.assertRaises(DownloadError):
                HttpTransport(session).get('https://cdn.example/a')
        self.assertEqual(session.get.call_count, 1)


if __name__ == '__main__':
    unittest.main()
