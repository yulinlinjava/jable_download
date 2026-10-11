"""Source-based JavaScript request signing for download-time URLs."""
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from Crypto.Cipher import AES


class SignerError(Exception):
    pass


DEFAULT_CODE = '''function sign(url, context, helpers, config) {
  const timestamp = helpers.now();
  const value = `${url}:${timestamp}`;
  const signature = helpers.hmacSha256(config.secret || '', value);

  return {
    url: helpers.withQuery(url, { timestamp, sign: signature }),
    headers: config.referer ? { Referer: config.referer } : {}
  };
}'''

DEFAULT_FORMAT_CODE = '''function format(raw, context, config) {
  const items = Array.isArray(raw) ? raw : (raw.data?.list || raw.items || []);
  return items.map(item => ({
    source: context.source,
    id: String(item.id || item.code || ''),
    title: String(item.title || item.name || '').trim(),
    url: item.url || item.detailUrl || null,
    m3u8_url: item.m3u8_url || item.video_url || item.videoUrl || item.playUrl,
    cover: item.cover || item.coverUrl || null,
    poster: item.poster || item.posterUrl || null,
    preview_image: item.preview_image || item.previewUrl || null,
    vtt_url: item.vtt_url || item.vttUrl || null,
    preview_m3u8_url: item.preview_m3u8_url || null,
    preview5_m3u8_url: item.preview5_m3u8_url || null,
    duration: item.duration == null ? null : String(item.duration),
    published_at: item.published_at || item.publishedAt || null,
    actors: Array.isArray(item.actors) ? item.actors : [],
    tags: Array.isArray(item.tags) ? item.tags : [],
    chapters: Array.isArray(item.chapters) ? item.chapters : []
  }));
}'''


class DownloadSignerManager:
    def __init__(self, database):
        self.database = Path(database)
        self.lock = threading.RLock()
        self.compiled = {}
        self.cache = {}
        self._key_cache = None
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS download_signers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL COLLATE NOCASE UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1, code TEXT NOT NULL, config TEXT,
                created REAL NOT NULL, updated REAL NOT NULL)''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(download_signers)')}
            for name in ('image_base_url', 'video_base_url', 'format_code'):
                if name not in columns:
                    db.execute(f"ALTER TABLE download_signers ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            if 'download_enabled' not in columns:
                db.execute('ALTER TABLE download_signers ADD COLUMN download_enabled INTEGER NOT NULL DEFAULT 1')
        self.reload()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _key(self):
        with self.lock:
            if self._key_cache is None:
                path = self.database.with_suffix('.sign.key')
                if not path.exists():
                    temporary = path.with_suffix(path.suffix + '.part')
                    temporary.write_bytes(os.urandom(32))
                    os.replace(temporary, path)
                    try:
                        os.chmod(path, 0o600)
                    except OSError:
                        pass
                self._key_cache = hashlib.sha256(path.read_bytes()).digest()
            return self._key_cache

    def _encrypt(self, value):
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        cipher = AES.new(self._key(), AES.MODE_GCM)
        encrypted, tag = cipher.encrypt_and_digest(raw)
        return base64.urlsafe_b64encode(cipher.nonce + tag + encrypted).decode()

    def _decrypt(self, value):
        if not value:
            return {}
        try:
            raw = base64.urlsafe_b64decode(value)
            cipher = AES.new(self._key(), AES.MODE_GCM, nonce=raw[:16])
            return json.loads(cipher.decrypt_and_verify(raw[32:], raw[16:32]).decode())
        except Exception as exc:
            raise SignerError('签名配置无法解密，请重新保存') from exc

    @staticmethod
    def validate(name, source, enabled, code, config):
        name, source, code = (name or '').strip(), (source or '').strip().lower(), (code or '').strip()
        if not name or len(name) > 100:
            raise SignerError('名称必须是 1–100 个字符')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', source):
            raise SignerError('source 只能包含英文字母、数字、下划线或连字符')
        if len(code) > 100000:
            raise SignerError('签名脚本不能超过 100000 个字符')
        if not isinstance(config, dict):
            raise SignerError('签名配置必须是 JSON 对象')
        encoded = json.dumps(config, ensure_ascii=False)
        if len(encoded) > 100000:
            raise SignerError('签名配置不能超过 100000 个字符')
        return name, source, bool(enabled), code, config

    @staticmethod
    def validate_options(image_base_url='', video_base_url='', format_code=''):
        bases = []
        for name, value in (('图片域名', image_base_url), ('视频域名', video_base_url)):
            value = str(value or '').strip().rstrip('/')
            if value:
                try:
                    parts = urlsplit(value)
                    if (parts.scheme not in ('http', 'https') or not parts.hostname
                            or parts.username or parts.password or parts.query or parts.fragment
                            or re.search(r'[\s\\\x00-\x1f]', value) or len(value) > 8192):
                        raise ValueError()
                    parts.port
                except ValueError:
                    raise SignerError(name + '必须是有效的 HTTP/HTTPS 基础地址') from None
            bases.append(value)
        format_code = str(format_code or '').strip()
        if len(format_code) > 100000:
            raise SignerError('格式化脚本不能超过 100000 个字符')
        return *bases, format_code

    def serialize(self, row, include_config=True):
        value = dict(id=row['id'], name=row['name'], source=row['source'],
                     enabled=bool(row['enabled']), code=row['code'],
                     image_base_url=row['image_base_url'], video_base_url=row['video_base_url'],
                     format_code=row['format_code'],
                     created_at=row['created'], updated_at=row['updated'])
        value['config'] = self._decrypt(row['config']) if include_config else {}
        return value

    def reload(self):
        with self.lock:
            with self.db() as db:
                rows = db.execute('SELECT * FROM download_signers').fetchall()
            self.cache = {row['source'].casefold(): self.serialize(row) for row in rows}
            self.compiled.clear()

    def list(self):
        with self.db() as db:
            rows = db.execute('SELECT * FROM download_signers ORDER BY created DESC').fetchall()
        return [self.serialize(row) for row in rows]

    def list_options(self):
        """Return the lightweight fields used by collection form selectors."""
        with self.db() as db:
            rows = db.execute('''SELECT id,name,source,enabled
                                 FROM download_signers ORDER BY created DESC''').fetchall()
        return [dict(id=row['id'], name=row['name'], source=row['source'],
                     enabled=bool(row['enabled']))
                for row in rows]

    def get(self, signer_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM download_signers WHERE id=?', (signer_id,)).fetchone()
        return self.serialize(row) if row else None

    def create(self, name, source, enabled, code, config, image_base_url='', video_base_url='',
               format_code=''):
        name, source, enabled, code, config = self.validate(name, source, enabled, code, config)
        image_base_url, video_base_url, format_code = self.validate_options(image_base_url, video_base_url, format_code)
        signer_id, now = uuid.uuid4().hex, time.time()
        try:
            with self.db() as db:
                db.execute('''INSERT INTO download_signers(id,name,source,enabled,code,config,created,updated,
                              image_base_url,video_base_url,format_code) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                           (signer_id, name, source, 1 if enabled else 0, code,
                            self._encrypt(config), now, now, image_base_url, video_base_url, format_code))
        except sqlite3.IntegrityError:
            raise SignerError('该 source 已经配置签名脚本') from None
        self.reload()
        return self.get(signer_id)

    def update(self, signer_id, name, source, enabled, code, config, image_base_url='',
               video_base_url='', format_code=''):
        name, source, enabled, code, config = self.validate(name, source, enabled, code, config)
        image_base_url, video_base_url, format_code = self.validate_options(image_base_url, video_base_url, format_code)
        try:
            with self.db() as db:
                changed = db.execute('''UPDATE download_signers SET name=?,source=?,enabled=?,code=?,
                                        config=?,image_base_url=?,video_base_url=?,format_code=?,updated=? WHERE id=?''',
                                     (name, source, 1 if enabled else 0, code, self._encrypt(config),
                                      image_base_url, video_base_url, format_code, time.time(), signer_id)).rowcount
        except sqlite3.IntegrityError:
            raise SignerError('该 source 已经配置签名脚本') from None
        if changed:
            self.reload()
        return self.get(signer_id) if changed else None

    def delete(self, signer_id):
        with self.db() as db:
            deleted = db.execute('DELETE FROM download_signers WHERE id=?', (signer_id,)).rowcount
        if deleted:
            self.reload()
        return bool(deleted)

    def definition(self, source):
        with self.lock:
            value = self.cache.get(str(source or '').casefold())
        return value if value and value['enabled'] else None

    @staticmethod
    def resource_group(resource_type, url):
        if resource_type in ('cover', 'poster', 'vtt_image'):
            return 'image'
        if resource_type == 'preview_image' and urlsplit(url).path.lower().endswith(
                ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif')):
            return 'image'
        return 'video'

    def resolve_url(self, source, url, resource_type='playlist', definition=None):
        definition = definition or self.definition(source)
        parts = urlsplit(str(url or ''))
        if parts.scheme in ('http', 'https') and parts.hostname:
            return url
        if parts.scheme or parts.netloc:
            raise SignerError('资源链接必须是 HTTP/HTTPS 地址或相对路径')
        group = self.resource_group(resource_type, url)
        base = (definition or {}).get(group + '_base_url')
        if not base:
            raise SignerError(f'来源 {source} 未配置{"图片" if group == "image" else "视频"}域名，无法访问相对路径')
        return urljoin(base + '/', url)

    def resolve_reference(self, source, reference, base_url, resource_type):
        resolved = urljoin(base_url, reference)
        definition = self.definition(source)
        parts = urlsplit(reference)
        group = self.resource_group(resource_type, resolved)
        if (not parts.scheme and not parts.netloc and definition
                and definition.get(group + '_base_url')):
            target = urlsplit(resolved)
            return self.resolve_url(source, urlunsplit(('', '', target.path, target.query, '')),
                                    resource_type, definition)
        return resolved

    def format_items(self, raw, source=None, apply_format=True, definition=None, allow_empty=False):
        """Format raw imports on the server; leave original resource paths unsigned."""
        if not source and apply_format and isinstance(raw, dict):
            source = raw.get('source')
        if source:
            source = str(source).strip().lower()
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', source):
                raise SignerError('source 格式无效')
            definition = definition or self.definition(source)
            code = (definition or {}).get('format_code') if apply_format else ''
            if code:
                try:
                    import quickjs
                    context = quickjs.Context()
                    context.set_memory_limit(32 * 1024 * 1024)
                    context.set_max_stack_size(512 * 1024)
                    context.set_time_limit(3)
                    context.eval(code)
                    payload = json.dumps([raw, {'source': source}, definition.get('config') or {}], ensure_ascii=False)
                    output = context.eval('''JSON.stringify((function(args) {
                      if (typeof format !== 'function') throw new Error('必须声明 format(raw, context, config)');
                      const result = format(args[0], args[1], args[2]);
                      if (result && typeof result.then === 'function') throw new Error('格式化函数必须同步返回，不能使用 async');
                      return result;
                    })(''' + payload + '))')
                    items = json.loads(output)
                except Exception as exc:
                    raise SignerError('格式化脚本执行失败：' + str(exc)[:300]) from None
            else:
                items = raw
            if not isinstance(items, list) or not (0 if allow_empty else 1) <= len(items) <= 1000:
                raise SignerError('格式化结果必须是包含 1–1000 条资源的数组')
            return [dict(item, source=source) if isinstance(item, dict) else item for item in items]
        if not isinstance(raw, list) or not (0 if allow_empty else 1) <= len(raw) <= 1000:
            raise SignerError('请提交包含 1–1000 条资源的数组，原始对象需要指定 source')
        if not apply_format:
            return raw
        # Keep adjacent groups in input order; sources may use different formatters.
        result, index = [], 0
        while index < len(raw):
            item = raw[index]
            item_source = str(item.get('source') or '').strip().lower() if isinstance(item, dict) else ''
            end = index + 1
            while end < len(raw) and isinstance(raw[end], dict) and str(raw[end].get('source') or '').strip().lower() == item_source:
                end += 1
            if self.definition(item_source):
                result.extend(self.format_items(raw[index:end], item_source, allow_empty=allow_empty))
            else:
                result.extend(raw[index:end])
            index = end
        if not (0 if allow_empty else 1) <= len(result) <= 1000:
            raise SignerError('格式化结果必须是包含 1–1000 条资源的数组')
        return result

    @staticmethod
    def _hash(algorithm, value):
        return hashlib.new(algorithm, str(value).encode()).hexdigest()

    @staticmethod
    def _hmac(algorithm, secret, value):
        return hmac.new(str(secret).encode(), str(value).encode(), algorithm).hexdigest()

    @staticmethod
    def _base64(value, urlsafe=False):
        function = base64.urlsafe_b64encode if urlsafe else base64.b64encode
        result = function(str(value).encode()).decode()
        return result.rstrip('=') if urlsafe else result

    @staticmethod
    def _with_query(url, params_json):
        try:
            params = json.loads(params_json)
            if not isinstance(params, dict):
                raise ValueError()
            parts = urlsplit(url)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query.update({str(key): str(value) for key, value in params.items() if value is not None})
            return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        except (TypeError, ValueError):
            raise SignerError('withQuery 参数无效') from None

    def _compile(self, definition):
        try:
            import quickjs
        except ImportError as exc:
            raise SignerError('服务缺少 quickjs-ng，无法执行签名脚本') from exc
        wrapper = definition['code'] + '''
function __signRequest(url, context, config) {
  if (typeof sign !== 'function') throw new Error('脚本必须声明 sign(url, context, helpers, config) 函数');
  const helpers = {
    now: () => Math.floor(Date.now() / 1000),
    md5: (value) => __hash('md5', String(value)),
    sha1: (value) => __hash('sha1', String(value)),
    sha256: (value) => __hash('sha256', String(value)),
    hmacSha1: (secret, value) => __hmac('sha1', String(secret), String(value)),
    hmacSha256: (secret, value) => __hmac('sha256', String(secret), String(value)),
    base64: (value) => __base64(String(value), false),
    base64Url: (value) => __base64(String(value), true),
    withQuery: (value, params) => __withQuery(String(value), JSON.stringify(params))
  };
  const result = sign(url, context, helpers, config);
  if (result && typeof result.then === 'function') throw new Error('下载签名函数必须同步返回，不能使用 async');
  return result;
}'''
        try:
            function = quickjs.Function('__signRequest', wrapper)
            function.set_memory_limit(8 * 1024 * 1024)
            function.set_max_stack_size(512 * 1024)
            function.add_callable('__hash', self._hash)
            function.add_callable('__hmac', self._hmac)
            function.add_callable('__base64', self._base64)
            function.add_callable('__withQuery', self._with_query)
            return function
        except Exception as exc:
            raise SignerError('签名脚本编译失败：' + str(exc)[:300]) from None

    def sign(self, source, url, context=None, definition=None):
        if definition is None:
            with self.lock:
                definition = self.cache.get((source or '').casefold())
            if not definition or not definition['enabled']:
                if urlsplit(url).scheme not in ('http', 'https'):
                    raise SignerError(f'来源 {source} 未配置域名，无法访问相对路径')
                return dict(url=url, headers={})
        original_url = url
        resource_type = (context or {}).get('resource_type', 'playlist')
        url = self.resolve_url(source, url, resource_type, definition)
        if not definition.get('code'):
            return dict(url=url, headers={})
        key = (definition['id'], definition['updated_at'])
        with self.lock:
            function = self.compiled.get(key)
            if function is None:
                function = self._compile(definition)
                self.compiled = {cached_key: cached for cached_key, cached in self.compiled.items()
                                 if cached_key[0] != definition['id']}
                self.compiled[key] = function
        payload = dict(context or {}, source=source, original_url=original_url,
                       resource_group=self.resource_group(resource_type, url))
        try:
            result = function(url, payload, definition.get('config') or {})
        except Exception as exc:
            raise SignerError('签名脚本执行失败：' + str(exc)[:300]) from None
        if isinstance(result, str):
            result = dict(url=result, headers={})
        if not isinstance(result, dict) or not isinstance(result.get('url'), str):
            raise SignerError('签名函数必须返回 URL 字符串或包含 url 的对象')
        signed_url = result['url'].strip()
        parts = urlsplit(signed_url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise SignerError('签名函数返回了无效的 HTTP/HTTPS URL')
        raw_headers = result.get('headers') or {}
        if not isinstance(raw_headers, dict) or len(raw_headers) > 30:
            raise SignerError('签名函数返回的 headers 无效')
        blocked = {'host', 'content-length', 'connection', 'transfer-encoding', 'range'}
        headers = {}
        for name, value in raw_headers.items():
            if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,64}', name)
                    or name.lower() in blocked or not isinstance(value, (str, int, float))
                    or len(str(value)) > 8192):
                raise SignerError('签名函数返回了不允许的请求头')
            headers[name] = str(value)
        return dict(url=signed_url, headers=headers)

    def test(self, name, source, enabled, code, config, url, resource_type='playlist',
             image_base_url='', video_base_url='', format_code=''):
        name, source, enabled, code, config = self.validate(name, source, enabled, code, config)
        image_base_url, video_base_url, format_code = self.validate_options(image_base_url, video_base_url, format_code)
        definition = dict(id='test', name=name, source=source, enabled=enabled, code=code,
                          image_base_url=image_base_url, video_base_url=video_base_url, format_code=format_code,
                          config=config, updated_at=time.time())
        return self.sign(source, url, dict(task_id='test', video_id='test',
                                           resource_type=resource_type), definition)
