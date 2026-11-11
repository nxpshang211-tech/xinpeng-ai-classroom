"""Verify the Base64 ASR request shape without making a network call."""

from __future__ import annotations

import base64
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import module as service


class FakeResponse:
    status_code = 200

    @staticmethod
    def json():
        return {"choices": [{"message": {"content": "模拟转写成功"}}]}


with tempfile.TemporaryDirectory(prefix="chapter07-asr-transport-") as temp_name:
    audio_path = Path(temp_name) / "chunk.mp3"
    source_bytes = b"fake-mp3-content"
    audio_path.write_bytes(source_bytes)
    object.__setattr__(service.settings, "dashscope_asr_transport", "base64")

    with patch.object(service.requests, "post", return_value=FakeResponse()) as post:
        result = service._transcribe_audio_file(audio_path, "probe-key")

    assert result == "模拟转写成功"
    _, kwargs = post.call_args
    assert kwargs["json"]["model"] == service.settings.dashscope_asr_model
    audio = kwargs["json"]["messages"][0]["content"][0]
    assert audio["type"] == "input_audio"
    data_uri = audio["input_audio"]["data"]
    assert data_uri.startswith("data:audio/mpeg;base64,")
    assert base64.b64decode(data_uri.split(",", 1)[1]) == source_bytes
    assert kwargs["json"]["stream"] is False
    assert kwargs["json"]["asr_options"] == {"enable_itn": True}
    assert kwargs["timeout"] == (20, 600)

print("base64 transport probe: OK")
