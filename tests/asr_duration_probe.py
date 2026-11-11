"""Verify that a small audio file is still split when it exceeds ASR duration."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import module as service


audio_path = next((service.settings.static_dir / "videos").glob("*.mp3"))
object.__setattr__(service.settings, "dashscope_api_key", "probe-key")

chunk_dir = Path(tempfile.mkdtemp(prefix="chapter07-asr-duration-"))
chunks = [chunk_dir / "chunk_001.mp3", chunk_dir / "chunk_002.mp3"]
for chunk in chunks:
    chunk.write_bytes(b"probe")
(chunk_dir / ".split_complete").write_text("2", encoding="ascii")

calls: list[str] = []


def transcribe(chunk: Path, _api_key: str) -> str:
    calls.append(chunk.name)
    return f"text-{chunk.name}"


with (
    patch.object(service, "_audio_duration_seconds", return_value=327),
    patch.object(
        service,
        "_split_audio_for_asr",
        return_value=(chunk_dir, chunks),
    ) as split,
    patch.object(service, "_transcribe_audio_file", side_effect=transcribe),
):
    transcript = service.audio_text(audio_path.name)

assert split.called
assert calls == ["chunk_001.mp3", "chunk_002.mp3"], calls
assert transcript.splitlines() == [
    "text-chunk_001.mp3",
    "text-chunk_002.mp3",
]
assert not chunk_dir.exists()
print("duration split probe: OK", calls)
