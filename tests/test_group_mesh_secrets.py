"""私钥保护层（`shell.groupmesh.secret_store`）的用例。

覆盖四件必须成立的事：

1. 口令派生（AES-256-GCM）能往返，改一个 bit 或换口令都必须报错；
2. 旧版明文 JSON 仍读得出来，并且在能提供更强保护时被**单向升级**；
3. 保护级别不会在读取时被静默降级（keyring 挂了不能把密文当明文）；
4. `Identity.init` / `Identity.load` 真的走这条路，而不是只测了工具函数。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shell.groupmesh import crypto_prims as cp
from shell.groupmesh import secret_store
from shell.groupmesh.identity import Identity


class _FakeKeyring:
    """最小 keyring 替身：不碰真实 OS keyring，保证 CI 可重复。"""

    def __init__(self) -> None:
        self.store: dict = {}

    def get_keyring(self):
        return self

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


class SecretStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        secret_store.reset_cache()

    def tearDown(self) -> None:
        secret_store.reset_cache()

    def _force(self, protector: str):
        return mock.patch.dict(os.environ, {secret_store.PROTECTOR_ENV: protector})

    def test_plain_is_not_an_envelope(self):
        with self._force('plain'):
            raw = b'{"private_key": "x"}'
            self.assertEqual(secret_store.protect(raw), raw)
            self.assertEqual(secret_store.unprotect(raw), raw)
            self.assertFalse(secret_store.is_protected(raw))
            self.assertFalse(secret_store.describe()['protected'])

    def test_legacy_plaintext_json_is_accepted(self):
        raw = json.dumps({'name': 'old', 'private_key': 'AAAA'}).encode('utf-8')
        self.assertEqual(secret_store.unprotect(raw), raw)

    def test_passphrase_roundtrip_and_tamper(self):
        with self._force('passphrase'), mock.patch.dict(
                os.environ, {secret_store.PASSPHRASE_ENV: 'correct horse battery staple'}):
            raw = b'{"secret": "value"}'
            envelope = secret_store.protect(raw)
            self.assertTrue(secret_store.is_protected(envelope))
            self.assertEqual(secret_store.unprotect(envelope), raw)
            payload = json.loads(envelope.decode('utf-8'))
            self.assertEqual(payload['protector'], 'passphrase')

            # 篡改密文 / 元数据都必须被 AEAD 拒绝
            broken = dict(payload)
            broken['data'] = base64.b64encode(b'\x00' * 64).decode('ascii')
            with self.assertRaises(secret_store.SecretError):
                secret_store.unprotect(json.dumps(broken).encode('utf-8'))

        # 换口令：解不开
        with self._force('passphrase'), mock.patch.dict(
                os.environ, {secret_store.PASSPHRASE_ENV: 'wrong'}), self.assertRaises(secret_store.SecretError):
            secret_store.unprotect(envelope)

    def test_keyring_roundtrip_with_fake_backend(self):
        fake = _FakeKeyring()
        with self._force('keyring'), mock.patch.object(
                secret_store, '_keyring_module', lambda: fake):
            raw = b'{"secret": "PLAINTEXT-MARKER"}'
            envelope = secret_store.protect(raw)
            self.assertEqual(json.loads(envelope.decode('utf-8'))['protector'], 'keyring')
            self.assertEqual(secret_store.unprotect(envelope), raw)
            # 明文不在文件里，密钥真的进了 keyring
            self.assertNotIn('PLAINTEXT-MARKER', envelope.decode('utf-8'))
            self.assertEqual(len(fake.store), 1)

    @unittest.skipUnless(sys.platform == 'win32', 'DPAPI 只在 Windows 上可用')
    def test_dpapi_roundtrip_and_tamper(self):
        with self._force('dpapi'):
            raw = b'{"secret": "dpapi"}'
            envelope = secret_store.protect(raw)
            self.assertTrue(secret_store.is_protected(envelope))
            self.assertEqual(secret_store.unprotect(envelope), raw)
            payload = json.loads(envelope.decode('utf-8'))
            import base64 as _b64
            blob = bytearray(_b64.b64decode(payload['data']))
            blob[0] ^= 0x01
            payload['data'] = _b64.b64encode(bytes(blob)).decode('ascii')
            with self.assertRaises(secret_store.SecretError):
                secret_store.unprotect(json.dumps(payload).encode('utf-8'))

    def test_best_protector_priority(self):
        with mock.patch.dict(os.environ, {
                secret_store.PASSPHRASE_ENV: 'p',
                secret_store.PROTECTOR_ENV: ''}):
            secret_store.reset_cache()
            self.assertEqual(secret_store.best_protector(), 'passphrase')
        with self._force('plain'):
            self.assertEqual(secret_store.best_protector(), 'plain')


class IdentitySecretTest(unittest.TestCase):
    def setUp(self) -> None:
        secret_store.reset_cache()

    def tearDown(self) -> None:
        secret_store.reset_cache()

    def test_identity_roundtrip_with_passphrase(self):
        with tempfile.TemporaryDirectory() as tmp, self._passphrase('pw'):
            identity = Identity.init(Path(tmp), 'alice', 'laptop')
            principal_file = Path(tmp) / 'principal.json'
            text = principal_file.read_text(encoding='utf-8')
            self.assertIn(secret_store.FORMAT, text)
            self.assertIn('"passphrase"', text)
            self.assertNotIn(cp.b64(identity.principal.private_key), text,
                             '私钥不能被明文写进文件')
            loaded = Identity.load(Path(tmp))
            self.assertEqual(loaded.principal.private_key, identity.principal.private_key)
            self.assertEqual(loaded.device.private_key, identity.device.private_key)

    def test_legacy_plaintext_is_migrated_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._force('plain'):
                identity = Identity.init(Path(tmp), 'legacy', 'old-pc')
            principal_file = Path(tmp) / 'principal.json'
            legacy = json.loads(principal_file.read_text(encoding='utf-8'))
            self.assertEqual(legacy['private_key'],
                             cp.b64(identity.principal.private_key))

            with self._passphrase('pw'):
                loaded = Identity.load(Path(tmp))
            self.assertEqual(loaded.principal.private_key, identity.principal.private_key)
            text = principal_file.read_text(encoding='utf-8')
            self.assertIn(secret_store.FORMAT, text, '加载旧明文时应升级成受保护格式')

    def test_migrate_never_downgrades_protected_file(self):
        with tempfile.TemporaryDirectory() as tmp, self._passphrase('pw'):
            Identity.init(Path(tmp), 'keep', 'pc')
            principal_file = Path(tmp) / 'principal.json'
            before = principal_file.read_bytes()
            with self._force('plain'):
                self.assertFalse(secret_store.migrate_file(principal_file))
            self.assertEqual(principal_file.read_bytes(), before)
            self.assertTrue(secret_store.is_protected(principal_file.read_bytes()))

    @staticmethod
    def _passphrase(value: str):
        class _Ctx:
            def __enter__(self):
                self._patch = mock.patch.dict(os.environ, {
                    secret_store.PASSPHRASE_ENV: value,
                    secret_store.PROTECTOR_ENV: 'passphrase',
                })
                self._patch.start()
                secret_store.reset_cache()
                return self

            def __exit__(self, *exc):
                self._patch.stop()
                secret_store.reset_cache()
                return False

        return _Ctx()

    def _force(self, protector: str):
        class _Ctx:
            def __enter__(self):
                self._patch = mock.patch.dict(os.environ, {
                    secret_store.PROTECTOR_ENV: protector})
                self._patch.start()
                secret_store.reset_cache()
                return self

            def __exit__(self, *exc):
                self._patch.stop()
                secret_store.reset_cache()
                return False

        return _Ctx()


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
