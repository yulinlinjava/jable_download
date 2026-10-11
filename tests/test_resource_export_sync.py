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
        self.addCleanup(self.sync.close)
        self.client = TestClient(api.app)
        for name, value in [('collections', self.collections), ('downloads', self.downloads),
                            ('resource_sync', self.sync)]:
            original = getattr(api.app.state, name, None)
            setattr(api.app.state, name, value)
            self.addCleanup(setattr, api.app.state, name, original)
        key = patch.object(api, 'API_KEY', 'test-secret')
        key.start(); self.addCleanup(key.stop)
        self.client.headers.update({'X-API-Key': 'test-secret'})
        self.task_id = self.collections.import_items([resource()])['results'][0]['task_id']

    def test_export_contract_includes_vtt(self):
        response = self.client.post('/api/collection-items/export', json={'ids': [self.task_id]})
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment;', response.headers['content-disposition'])
        exported = response.json()
        self.assertEqual(len(exported), 1)
        for key, value in resource().items():
            self.assertEqual(exported[0][key], value)
        self.assertEqual(exported[0]['chapters'], [])
        self.assertEqual(self.client.post('/api/collection-items/export',
                                         json={'ids': ['0' * 32]}).status_code, 404)

    @staticmethod
    def response(status, body):
        response = MagicMock(status_code=status)
        response.__enter__.return_value = response
        response.iter_content.return_value = [json.dumps(body).encode()]
        return response

    def test_sync_payload_headers_history_and_retry(self):
        settings = self.client.post('/api/resource-sync/sites', json={
            'name': 'Content site',
            'enabled': True, 'endpoint_url': 'https://api.example.com/resources/import',
            'request_headers': {
                'Authorization': 'Bearer private-token',
                'X-Tenant-ID': 'tenant-a',
            },
            'batch_size': 10, 'timeout': 15,
        }).json()['data']
        self.assertTrue(settings['has_request_headers'])
        self.assertNotIn('request_headers', settings)
        with self.downloads.db() as db:
            db.execute("UPDATE tasks SET status='completed' WHERE id=?", (self.task_id,))
        failed = self.response(400, {'message': 'invalid item'})
        with patch('resource_sync.requests.post', return_value=failed) as post:
            run = self.client.post('/api/resource-sync/execute', json={
                'site_id': settings['id'], 'ids': [self.task_id],
            }).json()['data']
        self.assertEqual((run['succeeded'], run['failed']), (0, 1))
        request = post.call_args
        self.assertEqual(request.kwargs['headers']['Authorization'], 'Bearer private-token')
        self.assertEqual(request.kwargs['headers']['X-Tenant-ID'], 'tenant-a')
        self.assertEqual(request.kwargs['json'][0]['vtt_url'], resource()['vtt_url'])
        succeeded_body = {'message': 'ok', 'results': [{
            'source': '123av', 'id': 'demo-001', 'status': 'success', 'remote_id': 'remote-1',
        }]}
        succeeded = self.response(200, succeeded_body)
        with patch('resource_sync.requests.post', return_value=succeeded):
            retry = self.client.post(f"/api/resource-sync/runs/{run['id']}/retry").json()['data']
        self.assertEqual((retry['succeeded'], retry['failed']), (1, 0))
        self.assertEqual(retry['items'][0]['remote_id'], 'remote-1')
        self.assertEqual(json.loads(retry['items'][0]['message']), succeeded_body)
        self.assertEqual(len(self.client.get('/api/resource-sync/runs').json()['data']), 2)

    def test_accepts_private_sync_endpoint_and_rejects_invalid_scheme(self):
        response = self.client.post('/api/resource-sync/sites', json={
            'name': 'Local site', 'enabled': True,
            'endpoint_url': 'http://127.0.0.1/import',
            'batch_size': 50, 'timeout': 30,
        })
        self.assertEqual(response.status_code, 200)
        response = self.client.post('/api/resource-sync/sites', json={
            'name': 'Invalid site', 'enabled': True,
            'endpoint_url': 'ftp://example.com/import',
            'batch_size': 50, 'timeout': 30,
        })
        self.assertEqual(response.status_code, 400)

    def test_legacy_auth_fields_are_ignored(self):
        response = self.client.post('/api/resource-sync/sites', json={
            'name': 'Legacy site', 'enabled': True,
            'endpoint_url': 'https://api.example.com/resources/import',
            'batch_size': 10, 'timeout': 15,
        })
        self.assertEqual(response.status_code, 200)
        site = response.json()['data']
        self.assertFalse(site['has_request_headers'])
        with self.sync.db() as db:
            db.execute("UPDATE resource_sync_sites SET auth_type='bearer',secret=? WHERE id=?",
                       (self.sync._encrypt('legacy-token'), site['id']))
        stored = self.sync.get_site(site['id'], include_secret=True)
        self.assertNotIn('Authorization', self.sync._headers(stored))

    def test_site_extra_headers_are_encrypted_preserved_and_sent(self):
        payload = {
            'name': 'Content site', 'enabled': True,
            'endpoint_url': 'https://api.example.com/resources/import',
            'batch_size': 10, 'timeout': 15,
            'request_headers': {
                'X-Tenant-ID': 'tenant-a',
                'Authorization': 'Bearer private-token',
            },
        }
        response = self.client.post('/api/resource-sync/sites', json=payload)
        self.assertEqual(response.status_code, 200)
        site = response.json()['data']
        self.assertTrue(site['has_request_headers'])
        self.assertNotIn('request_headers', site)
        stored = self.sync.get_site(site['id'], include_secret=True)
        self.assertNotIn('tenant-a', stored['request_headers'])
        headers = self.sync._headers(stored)
        self.assertEqual(headers['X-Tenant-ID'], 'tenant-a')
        self.assertEqual(headers['Authorization'], 'Bearer private-token')
        self.assertEqual(len([key for key in headers if key.casefold() == 'authorization']), 1)

        keep = dict(payload)
        keep.pop('request_headers')
        response = self.client.put(f"/api/resource-sync/sites/{site['id']}", json=keep)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['data']['has_request_headers'])
        self.assertEqual(self.sync._headers(
            self.sync.get_site(site['id'], include_secret=True))['X-Tenant-ID'], 'tenant-a')

        keep['request_headers'] = {}
        response = self.client.put(f"/api/resource-sync/sites/{site['id']}", json=keep)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['data']['has_request_headers'])
        self.assertNotIn('X-Tenant-ID', self.sync._headers(
            self.sync.get_site(site['id'], include_secret=True)))

        invalid = dict(payload, name='Invalid headers', request_headers={'Host': 'example.com'})
        self.assertEqual(self.client.post('/api/resource-sync/sites', json=invalid).status_code, 400)
        for request_headers in (
                {'X-Tenant': 'one', 'x-tenant': 'two'},
                {'X-Line': 'one\ntwo'},
                {'X-Space': ' leading'},
                {'X-Chinese': '中文'},
                {f'X-{index}': 'value' for index in range(31)}):
            invalid = dict(payload, name='Invalid headers', request_headers=request_headers)
            self.assertEqual(self.client.post('/api/resource-sync/sites', json=invalid).status_code, 400)

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
