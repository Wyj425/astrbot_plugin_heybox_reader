"""Offline authentication regressions; no platform network requests are made."""
import asyncio
import importlib.util
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        package_name = "_login_test_" + ROOT.name
        package = types.ModuleType(package_name)
        package.__path__ = [str(ROOT)]
        http = types.ModuleType("aiohttp")
        http.ClientSession = Mock(side_effect=AssertionError("Unexpected network request"))
        http.ClientTimeout = lambda **kwargs: types.SimpleNamespace(**kwargs)
        self.modules = patch.dict(sys.modules, {package_name: package, "aiohttp": http})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location(package_name + ".login", ROOT / "login.py")
        self.login = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.login
        spec.loader.exec_module(self.login)
        self.package_name = package_name
        self.service = types.SimpleNamespace(
            root=pathlib.Path(self.directory.name),
            cache=types.SimpleNamespace(values={"old": 1}, failures={"old": 1}),
            backend=types.SimpleNamespace(
                reader=types.SimpleNamespace(cache={"old": 1}, failed={"old": 1})),
            login_state={"state": "idle"}, login_task=None)

    def client(self, state="done", credentials=None):
        return types.SimpleNamespace(
            request_qr=AsyncMock(return_value=types.SimpleNamespace(qr_content="test-qr")),
            check_qr=AsyncMock(return_value=(
                types.SimpleNamespace(value=state), None, credentials)),
            check_credentials=AsyncMock(), close=AsyncMock())

    async def test_verification_closes_client_even_on_failure(self):
        client = self.client()
        client.check_credentials.side_effect = ValueError("expired")
        self.service.backend.heybox_client = lambda: client
        with self.assertRaises(ValueError):
            await self.login.verify(self.service)
        client.close.assert_awaited_once()

    async def test_qr_generation_starts_heybox_poll(self):
        client = self.client()
        module = types.ModuleType(self.package_name + ".heybox_client.api_client")
        module.XiaoheiheApiClient = Mock(return_value=client)
        with patch.dict(sys.modules, {module.__name__: module}), \
             patch.object(self.login, "qr_image", return_value="data:image/png;base64,AAAA"), \
             patch.object(self.login, "poll_heybox", new_callable=AsyncMock) as poll:
            await self.login.begin(self.service)
            self.assertEqual(self.service.login_state["state"], "scan")
            await self.service.login_task
            poll.assert_awaited_once_with(
                self.service, client, client.request_qr.return_value)

    async def test_confirmed_login_persists_credentials_and_clears_cache(self):
        from dataclasses import make_dataclass
        credentials = make_dataclass("TestCredentials", [("session", str)])("test-session")
        client = self.client(credentials=credentials)
        with patch.object(self.login.asyncio, "sleep", new_callable=AsyncMock):
            await self.login.poll_heybox(self.service, client, None)
        self.assertEqual(json.loads((self.service.root / "credentials.json").read_text()),
                         {"session": "test-session"})
        self.assertEqual(self.service.login_state["state"], "done")
        self.assertEqual(self.service.cache.values, {})
        self.assertEqual(self.service.backend.reader.cache, {})
        client.close.assert_awaited_once()

    async def test_expired_qr_closes_client_without_writing_credentials(self):
        client = self.client(state="expired")
        with patch.object(self.login.asyncio, "sleep", new_callable=AsyncMock):
            await self.login.poll_heybox(self.service, client, None)
        self.assertEqual(self.service.login_state["state"], "expired")
        self.assertFalse((self.service.root / "credentials.json").exists())
        client.close.assert_awaited_once()
