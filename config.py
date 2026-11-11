from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    base_dir: Path = BASE_DIR
    data_dir: Path = BASE_DIR / "data"
    static_dir: Path = BASE_DIR / "static"
    template_dir: Path = BASE_DIR / "templates"

    database_url: str = _env(
        "DATABASE_URL", f"sqlite:///{(BASE_DIR / 'data' / 'course.db').as_posix()}"
    )
    dashscope_api_key: str = _env("DASHSCOPE_API_KEY")
    dashscope_asr_model: str = _env("DASHSCOPE_ASR_MODEL", "qwen3-asr-flash")
    dashscope_text_model: str = _env("DASHSCOPE_TEXT_MODEL", "qwen-flash")
    dashscope_asr_transport: str = _env("DASHSCOPE_ASR_TRANSPORT", "base64")
    dashscope_compatible_base_url: str = _env(
        "DASHSCOPE_COMPATIBLE_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ).rstrip("/")
    dashscope_asr_chunk_seconds: int = int(
        _env("DASHSCOPE_ASR_CHUNK_SECONDS", "285")
    )
    dashscope_asr_max_mb: int = int(_env("DASHSCOPE_ASR_MAX_MB", "9"))
    dashscope_asr_retries: int = int(_env("DASHSCOPE_ASR_RETRIES", "5"))

    aliyun_search_api_key: str = _env("ALIYUN_SEARCH_API_KEY")
    aliyun_search_endpoint: str = _env("ALIYUN_SEARCH_ENDPOINT")
    max_upload_mb: int = int(_env("MAX_UPLOAD_MB", "500"))

    # Local demo defaults. Replace both values before a public deployment.
    token_aes_key: bytes = _env("TOKEN_AES_KEY", "chapter07-demo-k").encode("utf-8")
    token_aes_iv: bytes = _env("TOKEN_AES_IV", "chapter07-demo-i").encode("utf-8")


settings = Settings()

for directory in (
    settings.data_dir,
    settings.static_dir / "videos",
    settings.static_dir / "frames",
    settings.static_dir / "xminds",
):
    directory.mkdir(parents=True, exist_ok=True)

if len(settings.token_aes_key) not in (16, 24, 32):
    raise ValueError("TOKEN_AES_KEY 必须是 16、24 或 32 字节")
if len(settings.token_aes_iv) != 16:
    raise ValueError("TOKEN_AES_IV 必须是 16 字节")
