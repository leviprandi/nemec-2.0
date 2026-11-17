"""Acesso ao PostgreSQL: conexões curtas, uma transação por operação (nunca aberta durante o LLM)."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

SCHEMA = "nemec"


class Database:
    def __init__(self, url: str) -> None:
        self._url = url

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """Abre conexão própria (thread-safe por construção); commit ao sair, rollback na exceção."""
        conn = psycopg.connect(
            self._url,
            row_factory=dict_row,
            options=f"-c search_path={SCHEMA}",
            application_name="nemec",
        )
        try:
            with conn.transaction():
                yield conn
        finally:
            conn.close()
