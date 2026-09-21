import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import api
from collection_tasks import CollectionManager
from download_tasks import DownloadManager, HttpTransport
from resource_sync import ResourceSyncManager


def resource(**changes):
    value = dict(source='123av', id='demo-001', title='Example',
                 url='https://example.com/v/demo-001', cover='https://cdn.example.com/cover.jpg',
                 poster='https://cdn.example.com/poster.jpg', preview_image='https://cdn.example.com/preview.jpg',
                 vtt_url='https://cdn.example.com/preview.vtt', duration='01:30:00',
                 published_at='2026-09-18', actors=['Actor'], tags=['Tag'],
                 m3u8_url='https://cdn.example.com/index.m3u8')
    value.update(changes)
    return value


class ExportSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.downloads = DownloadManager(self.temp.name, start_enabled=False)
        self.addCleanup(self.downloads.close)
        self.collections = CollectionManager.__new__(CollectionManager)
        self.collections.database = self.downloads.database
        self.collections.downloads = self.downloads
        self.collections.scheduler_wake = threading.Event()
        self.sync = ResourceSyncManager(self.downloads.database, self.collections.export_items)
        self.client = TestClient(api.app)
        for name, value in [('collections', self.collections), ('downloads', self.downloads),
                            ('resource_sync', self.sync)]:
            original = getattr(api.app.state, name, None)
            setattr(api.app.state, name, value)
            self.addCleanup(setattr, api.app.state, name, original)
        key = patch.object(api, 'API_KEY', '')
        key.start(); self.addCleanup(key.stop)
        self.task_id = self.collections.import_items([resource()])['results'][0]['task_id']

    def test_export_contract_includes_vtt(self):
        response = self.client.post('/api/collection-items/export', json={'ids': [self.task_id]})
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment;', response.headers['content-disposition'])
        self.assertEqual(response.json(), [resource()])
        self.assertEqual(self.client.post('/api/collection-items/export',
                                         json={'ids': ['0' * 32]}).status_code, 404)

    @staticmethod
    def response(status, body):
        response = MagicMock(status_code=status)
        response.__enter__.return_value = response
        response.iter_content.return_value = [json.dumps(body).encode()]
        return response

    def test_sync_payload_auth_history_and_retry(self):
        settings = self.client.put('/api/resource-sync/settings', json={
            'enabled': True, 'endpoint_url': 'https://api.example.com/resources/import',
            'auth_type': 'bearer', 'header_name': 'X-API-Key', 'secret': 'private-token',
            'batch_size': 10, 'timeout': 15,
        }).json()['data']
        self.assertTrue(settings['has_secret'])
        self.assertNotIn('secret', settings)
        failed = self.response(400, {'message': 'invalid item'})
        with patch('resource_sync.public_url'), patch('resource_sync.requests.post', return_value=failed) as post:
            run = self.client.post('/api/resource-sync/execute', json={'ids': [self.task_id]}).json()['data']
        self.assertEqual((run['succeeded'], run['failed']), (0, 1))
        request = post.call_args
        self.assertEqual(request.kwargs['headers']['Authorization'], 'Bearer private-token')
        self.assertEqual(request.kwargs['json']['resources'][0]['vtt_url'], resource()['vtt_url'])
        succeeded = self.response(200, {'results': [{
            'source': '123av', 'id': 'demo-001', 'status': 'success', 'remote_id': 'remote-1',
        }]})
        with patch('resource_sync.public_url'), patch('resource_sync.requests.post', return_value=succeeded):
            retry = self.client.post(f"/api/resource-sync/runs/{run['id']}/retry").json()['data']
        self.assertEqual((retry['succeeded'], retry['failed']), (1, 0))
        self.assertEqual(retry['items'][0]['remote_id'], 'remote-1')
        self.assertEqual(len(self.client.get('/api/resource-sync/runs').json()['data']), 2)

    def test_rejects_private_sync_endpoint(self):
        response = self.client.put('/api/resource-sync/settings', json={
            'enabled': True, 'endpoint_url': 'http://127.0.0.1/import', 'auth_type': 'none',
            'header_name': 'X-API-Key', 'batch_size': 50, 'timeout': 30,
        })
        self.assertEqual(response.status_code, 400)

    def test_vtt_images_share_video_directory_without_preview_collision(self):
        folder = self.downloads.task_folder('123av', 'demo-001')
        folder.mkdir(parents=True)
        with self.downloads.db() as db:
            task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (self.task_id,)).fetchone())
        metadata = resource()
        vtt = b'WEBVTT\n\n00:00:00.000 --> 00:00:10.000\nsprite.jpg#xywh=0,0,160,90\n'
        def fetch(url, **kwargs):
            return vtt if url.endswith('.vtt') else b'\xff\xd8\xffimage'
        with patch.object(HttpTransport, 'get', side_effect=fetch):
            with __import__('requests').Session() as session:
                warnings = self.downloads.assets(task, folder, metadata, HttpTransport(session))
        self.assertNotIn('VTT 或进度缩略图下载失败', warnings)
        self.assertTrue((folder / 'preview_image.jpg').is_file())
        self.assertTrue((folder / 'vtt-preview-001.jpg').is_file())
        self.assertFalse((folder / 'thumbnails').exists())
        content = (folder / 'preview.vtt').read_text()
        self.assertIn('vtt-preview-001.jpg#xywh=0,0,160,90', content)
        self.assertNotIn('thumbnails/', content)


if __name__ == '__main__':
    unittest.main()
