"""Verify segmented ASR can resume after a connection reset without API calls."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import module as service


audio_path = next((PROJECT_DIR / "static" / "videos").glob("*.mp3"))
object.__setattr__(service.settings, "dashscope_api_key", "probe-key")
calls_before_failure: list[str] = []


def fail_on_second_chunk(path: Path, _api_key: str) -> str:
    calls_before_failure.append(path.name)
    if len(calls_before_failure) == 2:
        raise ConnectionResetError(10054, "probe connection reset")
    return f"text-{path.name}"


with patch.object(
    service, "_transcribe_audio_file", side_effect=fail_on_second_chunk
):
    try:
        service.audio_text(audio_path.name)
    except service.PipelineError as exc:
        assert "第 2/" in str(exc), exc
    else:
        raise AssertionError("The simulated reset did not fail the second chunk")

chunk_dir = service.settings.data_dir / "asr_chunks" / audio_path.stem
cache = json.loads((chunk_dir / "transcripts.json").read_text(encoding="utf-8"))
assert list(cache) == ["chunk_001.mp3"], cache

calls_after_retry: list[str] = []


def succeed(path: Path, _api_key: str) -> str:
    calls_after_retry.append(path.name)
    return f"text-{path.name}"


with patch.object(service, "_transcribe_audio_file", side_effect=succeed):
    transcript = service.audio_text(audio_path.name)

assert "chunk_001.mp3" not in calls_after_retry, calls_after_retry
assert len(transcript.splitlines()) == len(calls_after_retry) + 1
assert not chunk_dir.exists()
print(
    "resume probe: OK",
    {
        "failed_at": calls_before_failure[-1],
        "cached": "chunk_001.mp3",
        "remaining_after_retry": len(calls_after_retry),
    },
)
