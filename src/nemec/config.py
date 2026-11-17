"""Configuração a partir do ambiente/.env. Todo destino de rede deve ser localhost (G9)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from psycopg.conninfo import conninfo_to_dict

from nemec.errors import ConfigError

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_CHAT_MODEL = "qwen3.5:9b"
DEFAULT_EMBED_MODEL = "bge-m3"
EMBED_DIM = 1024  # fixado na migração 0002
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_dotenv(path: Path, environ: dict[str, str] | None = None) -> None:
    """Carrega KEY=VALUE sem sobrescrever variáveis já definidas."""
    env = os.environ if environ is None else environ
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        env.setdefault(key.strip(), value)


def assert_loopback_url(url: str, *, what: str) -> None:
    host = urlparse(url).hostname
    if host not in LOOPBACK_HOSTS:
        raise ConfigError(f"{what}: só é permitido localhost (recebido: {host!r}).")


def assert_loopback_dsn(dsn: str, *, what: str) -> None:
    host = conninfo_to_dict(dsn).get("host")
    if host not in LOOPBACK_HOSTS:
        raise ConfigError(f"{what}: só é permitido localhost (recebido: {host!r}).")


@dataclass(frozen=True)
class Settings:
    database_url: str
    ollama_host: str = DEFAULT_OLLAMA_HOST
    chat_model: str = DEFAULT_CHAT_MODEL
    embed_model: str = DEFAULT_EMBED_MODEL
    num_ctx: int = 8192
    context_chars: int = 24_000
    memory_chars: int = 4_000
    think: bool = False
    retrieval_chars: int = 6_000  # orçamento de texto das passagens recuperadas no prompt
    retrieval_k: int = 6

    def __post_init__(self) -> None:
        assert_loopback_dsn(self.database_url, what="NEMEC_DATABASE_URL")
        assert_loopback_url(self.ollama_host, what="NEMEC_OLLAMA_HOST")
        if not self.chat_model.strip():
            raise ConfigError("NEMEC_CHAT_MODEL vazio.")
        if not self.embed_model.strip():
            raise ConfigError("NEMEC_EMBED_MODEL vazio.")


def admin_url(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    url = env.get("NEMEC_ADMIN_URL")
    if not url:
        raise ConfigError("NEMEC_ADMIN_URL não definido (rode `make setup`).")
    assert_loopback_dsn(url, what="NEMEC_ADMIN_URL")
    return url


def load_settings(environ: dict[str, str] | None = None, *, dotenv: Path | None = None) -> Settings:
    env = os.environ if environ is None else environ
    load_dotenv(dotenv if dotenv is not None else project_root() / ".env", env)
    url = env.get("NEMEC_DATABASE_URL")
    if not url:
        raise ConfigError("NEMEC_DATABASE_URL não definido (rode `make setup`).")
    return Settings(
        database_url=url,
        ollama_host=env.get("NEMEC_OLLAMA_HOST", DEFAULT_OLLAMA_HOST),
        chat_model=env.get("NEMEC_CHAT_MODEL", DEFAULT_CHAT_MODEL),
        embed_model=env.get("NEMEC_EMBED_MODEL", DEFAULT_EMBED_MODEL),
    )
