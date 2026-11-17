"""Diagnóstico de instalação (somente leitura): `make doctor`.

Verifica Python, `.env`, PostgreSQL (migrações, pgvector, papel `nemec_app`) e Ollama (modelos).
Não grava no banco, não baixa modelos e não altera nada. Sai com 0 se tudo estiver pronto, 1 caso contrário.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

OK, FAIL = "OK   ", "FALHA"
problems = 0


def report(ok: bool, what: str, hint: str = "") -> None:
    global problems
    print(f"[{OK if ok else FAIL}] {what}" + ("" if ok or not hint else f"\n        → {hint}"))
    problems += 0 if ok else 1


report(sys.version_info[:2] == (3, 12), f"Python {sys.version.split()[0]} (requer 3.12)",
       "use `make setup` (cria a .venv com python3.12; `brew install python@3.12`)")

from nemec import config as cfg  # noqa: E402
from nemec.errors import ConfigError  # noqa: E402

try:
    settings = cfg.load_settings()
    report(True, ".env e configuração válidos (somente localhost)")
except ConfigError as exc:
    report(False, f"configuração: {exc}", "rode `make setup` (cria o .env com senhas geradas)")
    sys.exit(1)

import psycopg  # noqa: E402

try:
    with psycopg.connect(settings.database_url, connect_timeout=5) as conn:
        report(True, "PostgreSQL acessível como nemec_app")
        row = conn.execute(
            "select rolsuper, rolcreatedb, rolcreaterole from pg_roles where rolname = current_user"
        ).fetchone()
        report(row == (False, False, False), "papel nemec_app sem privilégios administrativos",
               "recrie o banco com `make db-up` num volume novo (db/roles.sql)")
        has_vec = conn.execute("select 1 from pg_extension where extname = 'vector'").fetchone()
        report(bool(has_vec), "extensão pgvector instalada", "rode `make migrate`")
        # nemec_app não lê `alembic_version` (pertence ao nemec_owner): confere o esquema da última migração (0003).
        found = {r[0] for r in conn.execute(
            "select table_name || '.' || column_name from information_schema.columns "
            "where table_schema = 'nemec' and (table_name, column_name) in "
            "(('messages','seq'), ('document_chunks','embedding'), ('conversations','is_pinned'))"
        )}
        report(len(found) == 3, "migrações aplicadas (esquema completo, até 0003)", "rode `make migrate`")
except psycopg.Error as exc:
    report(False, f"PostgreSQL inacessível: {str(exc).splitlines()[0]}", "rode `make db-up` (Docker em execução?)")

try:
    import ollama

    client = ollama.Client(host=settings.ollama_host, timeout=5)
    names = {m.model for m in client.list().models}
    report(True, f"Ollama acessível em {settings.ollama_host}")
    for kind, model in (("chat", settings.chat_model), ("embeddings", settings.embed_model)):
        present = model in names or f"{model}:latest" in names
        report(present, f"modelo de {kind} '{model}' instalado", f"rode `ollama pull {model}` (o Nemec não baixa modelos)")
except Exception as exc:  # httpx.ConnectError, ollama.ResponseError…
    report(False, f"Ollama inacessível em {settings.ollama_host}: {type(exc).__name__}",
           "inicie o Ollama (aplicativo ou `ollama serve`)")

print("\nTudo pronto." if not problems else f"\n{problems} problema(s) encontrado(s).")
sys.exit(1 if problems else 0)
