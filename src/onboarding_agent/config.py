"""Shared settings loader.

Each variable is looked up in this order: process environment, the repo-root
`.env` file, then `st.secrets`. Empty strings count as unset, so a blank
GitHub Actions secret falls through to the next source.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, field_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
DOTENV_PATH = REPO_ROOT / ".env"


class ConfigError(ValueError):
    """A configured value cannot be used. The message is user-facing Korean."""


class MissingCredential(ConfigError):
    """A credential points to a file that does not exist yet."""


class Settings(BaseModel):
    """Typed view of the variables documented in `.env.example`."""

    model_config = ConfigDict(frozen=True, validate_default=True)

    # LLM (shared)
    gemini_api_key: SecretStr | None = None
    gemini_model_primary: str | None = None
    gemini_model_fallbacks: list[str] = []
    llm_timeout_sec: float = 60.0
    force_fallback: bool = False

    # Google
    google_service_account_json: SecretStr | None = None
    gcal_calendar_id: str | None = None
    gsheets_spreadsheet_id: str | None = None

    # Slack
    slack_bot_token: SecretStr | None = None
    slack_channel_id: str | None = None

    # Operations
    state_backend: Literal["sqlite", "sheets"] = "sqlite"
    sqlite_path: Path = Path("data/state.db")
    actions_dry_run: bool = True
    timezone: str = "Asia/Seoul"
    reminder_stages: list[str] = ["D-1", "D-day", "overdue"]
    meeting_max_chars: int = 100_000
    roster_path: Path = Path("data/roster.yaml")

    # Embeddings (separate from the generation model chain) and regulation Q&A
    gemini_embed_model: str = "gemini-embedding-001"
    gemini_embed_dim: int = 3072
    gemini_embed_query_task: Literal["QUESTION_ANSWERING", "RETRIEVAL_QUERY"] = "QUESTION_ANSWERING"
    rag_top_k: int = 6
    rag_min_score: float | None = None  # None: the default chosen on the dev set (rag.retrieve)
    rag_retrieval_mode: Literal["vector", "bm25", "rrf"] | None = None  # None: the dev-set choice
    rag_max_agent_steps: int = 4
    rag_enforce_budget: bool = True  # block calls past the build-task budget (usage is logged either way)
    reg_index_dir: Path = Path("data/index/regulations")
    law_api_oc: SecretStr | None = None
    law_api_base: str = "https://www.law.go.kr/DRF"

    @field_validator("gemini_model_fallbacks", "reminder_stages", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("sqlite_path", "roster_path", "reg_index_dir")
    @classmethod
    def _resolve_from_repo_root(cls, value: Path) -> Path:
        return value if value.is_absolute() else REPO_ROOT / value

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown time zone") from None
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def gemini_models(self) -> list[str]:
        """Model chain in try order: primary first, then fallbacks, no duplicates."""
        chain = [self.gemini_model_primary, *self.gemini_model_fallbacks]
        return list(dict.fromkeys(name for name in chain if name))


def env_var_names() -> list[str]:
    return [name.upper() for name in Settings.model_fields]


def _streamlit_secrets() -> Mapping[str, Any]:
    # Only consult st.secrets when the app already imported streamlit, so the
    # batch path never pulls in streamlit.
    if "streamlit" not in sys.modules:
        return {}
    try:
        import streamlit as st

        return {key: value for key, value in st.secrets.items() if not isinstance(value, Mapping)}
    except Exception:  # no secrets.toml, or not running inside streamlit
        return {}


def load_settings(
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
    secrets: Mapping[str, Any] | None = None,
) -> Settings:
    """Build settings from the three sources. Raises ConfigError on bad values."""
    environ = os.environ if environ is None else environ
    dotenv_path = DOTENV_PATH if dotenv_path is None else dotenv_path
    file_values = dotenv_values(dotenv_path) if dotenv_path.is_file() else {}
    secrets = _streamlit_secrets() if secrets is None else secrets

    raw: dict[str, str] = {}
    for field_name in Settings.model_fields:
        key = field_name.upper()
        for source in (environ, file_values, secrets):
            value = source.get(key)
            if value is not None and str(value).strip():
                raw[field_name] = str(value).strip()
                break

    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        # Report variable names and reasons only; never echo the values.
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']).upper()}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(f"설정값을 확인하세요 — {problems}") from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


def load_service_account_info(settings: Settings | None = None) -> dict[str, Any] | None:
    """Parse GOOGLE_SERVICE_ACCOUNT_JSON given as a file path, raw JSON, or base64 JSON.

    Returns None when the variable is unset. Raises MissingCredential when it
    names a file that does not exist, and ConfigError when it cannot be parsed.
    """
    settings = settings or get_settings()
    if settings.google_service_account_json is None:
        return None
    value = settings.google_service_account_json.get_secret_value().strip()

    if value.startswith("{"):
        return _parse_service_account(value, "JSON 문자열")

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    if path.is_file():
        return _parse_service_account(path.read_text(encoding="utf-8"), "파일")

    decoded = _decode_base64(value)
    if decoded is not None:
        return _parse_service_account(decoded, "base64 문자열")

    if value.endswith(".json") or "/" in value or os.sep in value:
        raise MissingCredential(f"서비스 계정 키 파일이 없습니다: {value}")
    raise ConfigError(
        "GOOGLE_SERVICE_ACCOUNT_JSON을 해석할 수 없습니다. "
        "파일 경로, JSON 문자열, base64 문자열 중 하나로 넣어 주세요."
    )


def _decode_base64(value: str) -> str | None:
    try:
        text = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return text if text.lstrip().startswith("{") else None


def _parse_service_account(text: str, source: str) -> dict[str, Any]:
    try:
        info = json.loads(text)
    except json.JSONDecodeError:
        raise ConfigError(f"서비스 계정 키({source})가 올바른 JSON이 아닙니다.") from None
    if not isinstance(info, dict) or "client_email" not in info:
        raise ConfigError(f"서비스 계정 키({source})에 client_email이 없습니다.")
    return info
