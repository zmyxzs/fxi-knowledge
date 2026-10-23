import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def runner(tmp_path):
    path = Path(__file__).resolve().parents[1] / "data/validation/2026-10-14-style-prompt-delivery/synthesize_learning.py"
    spec = importlib.util.spec_from_file_location("validation_style_batches", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.OUT = tmp_path
    return module


def test_preflight_keeps_every_batch_even_when_capture_stops_each_call(runner):
    capture = runner.CaptureAll(RuntimeError)

    async def run():
        return await asyncio.gather(*(capture.chat([{"content": text}]) for text in ("one", "two")),
                                    return_exceptions=True)

    assert all(isinstance(result, RuntimeError) for result in asyncio.run(run()))
    assert capture.requests == [[{"content": "one"}], [{"content": "two"}]]


def test_raw_response_survives_before_consumer_and_is_reused(runner):
    def write_once(path, value):
        with path.open("x", encoding="utf-8") as stream:
            json.dump(value, stream)

    owner = SimpleNamespace(
        _style_hash=lambda value: hashlib.sha256(json.dumps(value).encode()).hexdigest(),
        write_once=write_once,
        load_json=lambda path: json.loads(path.read_text(encoding="utf-8")),
    )

    class Delegate:
        calls = 0

        async def chat(self, messages, *, json_mode=False):
            self.calls += 1
            return SimpleNamespace(text="complete response", provider_model="test", usage={})

    delegate = Delegate()
    messages = [{"content": "exact batch"}]
    guard = runner.SealedBatches(delegate, owner, [messages])
    assert asyncio.run(guard.chat(messages)).text == "complete response"
    assert len(list(runner.OUT.glob("raw-response-*.json"))) == 1
    assert asyncio.run(guard.chat(messages)).text == "complete response"
    assert delegate.calls == 1
    with pytest.raises(RuntimeError, match="differs"):
        asyncio.run(guard.chat([{"content": "changed"}]))


def test_unresolved_batch_is_not_silently_retried(runner):
    owner = SimpleNamespace(_style_hash=lambda value: "fixed")
    (runner.OUT / "request-intent-fixed.json").touch()
    guard = runner.SealedBatches(None, owner, [[]])
    with pytest.raises(RuntimeError, match="unresolved"):
        asyncio.run(guard.chat([]))
