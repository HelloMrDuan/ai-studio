import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
GEMMA_PATH = ROOT / "app/services/gemma.py"
config_before = sys.modules.get("app.config")
config_stub = types.ModuleType("app.config")
config_stub.Settings = type("Settings", (), {})
sys.modules["app.config"] = config_stub
try:
    spec = importlib.util.spec_from_file_location("v23963_gemma_under_test", GEMMA_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load app/services/gemma.py")
    gemma_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gemma_module)
finally:
    if config_before is None:
        sys.modules.pop("app.config", None)
    else:
        sys.modules["app.config"] = config_before
GemmaService = gemma_module.GemmaService


class _FakeResponse:
    def __init__(self, status_code: int, body: dict, text: str = "") -> None:
        self.status_code = status_code
        self._body = body
        self.text = text
        self.request = httpx.Request(
            "POST",
            "http://127.0.0.1:6006/v1/chat/completions",
        )

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=self.request,
                response=self,
            )

    def json(self) -> dict:
        return self._body


class _RecordingAsyncClient:
    responses: list[_FakeResponse] = []
    payloads: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def post(self, url: str, *, json: dict):
        self.__class__.payloads.append(json)
        return self.__class__.responses.pop(0)


class QwenRequestCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _RecordingAsyncClient.responses = []
        _RecordingAsyncClient.payloads = []
        settings = SimpleNamespace(
            data_dir=".",
            gemma_base_url="http://127.0.0.1:6006/v1",
            gemma_timeout_seconds=30,
        )
        self.service = GemmaService(settings)

    @staticmethod
    def _success(content: str = "QWEN_OK") -> _FakeResponse:
        return _FakeResponse(
            200,
            {
                "model": "qwen3-32b",
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                "timings": {"prompt_n": 12, "predicted_n": 3},
            },
        )

    async def _request(self, messages, verified_model: str = "qwen3-32b"):
        with patch.object(gemma_module.httpx, "AsyncClient", _RecordingAsyncClient):
            return await self.service._request_messages(
                messages=messages,
                temperature=0.2,
                max_tokens=321,
                verified_model=verified_model,
            )

    async def test_qwen3_32b_normal_call_uses_minimal_payload(self):
        _RecordingAsyncClient.responses = [self._success()]

        content, model, metrics = await self._request(
            [{"role": "user", "content": "hello"}]
        )

        self.assertEqual((content, model), ("QWEN_OK", "qwen3-32b"))
        self.assertEqual(
            set(_RecordingAsyncClient.payloads[0]),
            {"model", "messages", "temperature", "max_tokens", "stream"},
        )
        self.assertEqual(_RecordingAsyncClient.payloads[0]["model"], "qwen3-32b")
        self.assertEqual(metrics["usage"]["prompt_tokens"], 12)
        self.assertEqual(metrics["timings"]["predicted_n"], 3)
        self.assertEqual(metrics["request_attempts"], 1)

    async def test_gemma_alias_is_normalized_to_qwen3_32b(self):
        _RecordingAsyncClient.responses = [self._success()]

        await self._request([{"role": "user", "content": "hello"}], "gemma")

        self.assertEqual(_RecordingAsyncClient.payloads[0]["model"], "qwen3-32b")

    async def test_message_list_content_is_converted_to_text(self):
        _RecordingAsyncClient.responses = [self._success()]

        await self._request(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "first"},
                        {"type": "text", "text": "second"},
                    ],
                }
            ]
        )

        self.assertEqual(
            _RecordingAsyncClient.payloads[0]["messages"][0]["content"],
            "first\nsecond",
        )

    async def test_null_content_is_converted_to_empty_string(self):
        _RecordingAsyncClient.responses = [self._success()]

        await self._request([{"role": "user", "content": None}])

        self.assertEqual(
            _RecordingAsyncClient.payloads[0]["messages"][0]["content"],
            "",
        )

    async def test_missing_role_is_rejected_before_request(self):
        with self.assertRaisesRegex(ValueError, "role"):
            await self._request([{"content": "hello"}])
        self.assertEqual(_RecordingAsyncClient.payloads, [])

    async def test_http_400_logs_shape_and_retries_minimal_payload(self):
        _RecordingAsyncClient.responses = [
            _FakeResponse(400, {}, '{"error":"invalid request"}'),
            self._success("RECOVERED"),
        ]

        with patch.object(gemma_module.asyncio, "sleep", new=AsyncMock()) as sleep:
            with self.assertLogs("v23963_gemma_under_test", level="WARNING") as logs:
                content, model, metrics = await self._request(
                    [{"role": "user", "content": ["hello", "world"]}]
                )

        self.assertEqual((content, model), ("RECOVERED", "qwen3-32b"))
        self.assertEqual(len(_RecordingAsyncClient.payloads), 2)
        for payload in _RecordingAsyncClient.payloads:
            self.assertEqual(
                set(payload),
                {"model", "messages", "temperature", "max_tokens", "stream"},
            )
        self.assertEqual(metrics["request_attempts"], 2)
        self.assertEqual(metrics["request_retries"], 1)
        self.assertIn("qwen3-32b", logs.output[0])
        self.assertIn("roles=['user']", logs.output[0])
        self.assertIn("content_lengths=[11]", logs.output[0])
        self.assertIn('{"error":"invalid request"}', logs.output[0])
        sleep.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
