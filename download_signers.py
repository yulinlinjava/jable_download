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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


class DownloadSignerManager:
    def __init__(self, database):
        self.database = Path(database)
        self.lock = threading.RLock()
        self.compiled = {}
        self.cache = {}
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS download_signers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL COLLATE NOCASE UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1, code TEXT NOT NULL, config TEXT,
                created REAL NOT NULL, updated REAL NOT NULL)''')
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
        path = self.database.with_suffix('.sign.key')
        if not path.exists():
            temporary = path.with_suffix(path.suffix + '.part')
            temporary.write_bytes(os.urandom(32))
            os.replace(temporary, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return hashlib.sha256(path.read_bytes()).digest()

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
        if not code or len(code) > 100000:
            raise SignerError('签名脚本必须是 1–100000 个字符')
        if not isinstance(config, dict):
            raise SignerError('签名配置必须是 JSON 对象')
        encoded = json.dumps(config, ensure_ascii=False)
        if len(encoded) > 100000:
            raise SignerError('签名配置不能超过 100000 个字符')
        return name, source, bool(enabled), code, config

    def serialize(self, row, include_config=True):
        value = dict(id=row['id'], name=row['name'], source=row['source'],
                     enabled=bool(row['enabled']), code=row['code'],
                     created_at=row['created'], updated_at=row['updated'])
        value['config'] = self._decrypt(row['config']) if include_config else {}
        return value

    def reload(self):
        with self.db() as db:
            rows = db.execute('SELECT * FROM download_signers').fetchall()
        values = {row['source'].casefold(): self.serialize(row) for row in rows}
        with self.lock:
            self.cache = values
            self.compiled.clear()

    def list(self):
        with self.db() as db:
            rows = db.execute('SELECT * FROM download_signers ORDER BY updated DESC').fetchall()
        return [self.serialize(row) for row in rows]

    def get(self, signer_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM download_signers WHERE id=?', (signer_id,)).fetchone()
        return self.serialize(row) if row else None

    def create(self, name, source, enabled, code, config):
        name, source, enabled, code, config = self.validate(name, source, enabled, code, config)
        signer_id, now = uuid.uuid4().hex, time.time()
        try:
            with self.db() as db:
                db.execute('''INSERT INTO download_signers(id,name,source,enabled,code,config,created,updated)
                              VALUES(?,?,?,?,?,?,?,?)''',
                           (signer_id, name, source, 1 if enabled else 0, code,
                            self._encrypt(config), now, now))
        except sqlite3.IntegrityError:
            raise SignerError('该 source 已经配置签名脚本') from None
        self.reload()
        return self.get(signer_id)

    def update(self, signer_id, name, source, enabled, code, config):
        name, source, enabled, code, config = self.validate(name, source, enabled, code, config)
        try:
            with self.db() as db:
                changed = db.execute('''UPDATE download_signers SET name=?,source=?,enabled=?,code=?,
                                        config=?,updated=? WHERE id=?''',
                                     (name, source, 1 if enabled else 0, code, self._encrypt(config),
                                      time.time(), signer_id)).rowcount
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
                return dict(url=url, headers={})
        key = (definition['id'], definition['updated_at'])
        with self.lock:
            function = self.compiled.get(key)
            if function is None:
                function = self._compile(definition)
                self.compiled = {cached_key: cached for cached_key, cached in self.compiled.items()
                                 if cached_key[0] != definition['id']}
                self.compiled[key] = function
        payload = dict(context or {}, source=source, original_url=url)
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

    def test(self, name, source, enabled, code, config, url, resource_type='playlist'):
        name, source, enabled, code, config = self.validate(name, source, enabled, code, config)
        definition = dict(id='test', name=name, source=source, enabled=enabled, code=code,
                          config=config, updated_at=time.time())
        return self.sign(source, url, dict(task_id='test', video_id='test',
                                           resource_type=resource_type), definition)
