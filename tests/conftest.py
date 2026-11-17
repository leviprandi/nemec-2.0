"""Fixtures: banco temporário migrado do zero (papéis do cluster reais), backend falso."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from urllib.parse import urlparse

from psycopg.rows import dict_row

from nemec import config as cfg
from nemec import dbadmin
from nemec.backend import CancelToken
from nemec.config import Settings
from nemec.db import Database
from nemec.documents import DocumentService, Retriever
from nemec.services import ChatService, MemoryService, TaskService
from tests.fakes import HashEmbedder

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BOOTSTRAP = (cfg.project_root() / "db" / "bootstrap.sql").read_text(encoding="utf-8")


def _with_db(url: str, dbname: str) -> str:
    return urlparse(url)._replace(path=f"/{dbname}").geturl()


@pytest.fixture(scope="session")
def env() -> dict[str, str]:
    cfg.load_dotenv(cfg.project_root() / ".env")
    return dict(os.environ)


@pytest.fixture(scope="session")
def admin_url(env) -> str:
    url = cfg.admin_url(env)
    try:
        psycopg.connect(url, connect_timeout=5).close()
    except psycopg.OperationalError as exc:  # falha explícita, nunca skip
        pytest.fail(f"PostgreSQL indisponível (rode `make db-up`): {exc}", pytrace=False)
    return url


@pytest.fixture(scope="session")
def test_dbname(admin_url) -> str:
    name = f"nemec_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin_url, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    with psycopg.connect(_with_db(admin_url, name), autocommit=True) as c:
        c.execute(BOOTSTRAP)
    dbadmin.migrate(_with_db(admin_url, name))
    yield name
    with psycopg.connect(admin_url, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def test_admin_url(admin_url, test_dbname) -> str:
    return _with_db(admin_url, test_dbname)


@pytest.fixture(scope="session")
def test_app_url(env, test_dbname) -> str:
    return _with_db(env["NEMEC_DATABASE_URL"], test_dbname)


@pytest.fixture()
def admin_conn(test_admin_url):
    with psycopg.connect(test_admin_url, autocommit=True, row_factory=dict_row, options="-c search_path=nemec") as c:
        c.execute("TRUNCATE action_proposals, memories, messages, conversations, tasks, documents CASCADE")
        yield c


@pytest.fixture()
def db(test_app_url, admin_conn) -> Database:
    return Database(test_app_url)


@pytest.fixture()
def settings(test_app_url) -> Settings:
    return Settings(database_url=test_app_url, chat_model="modelo-teste")


@pytest.fixture()
def memory_service(db) -> MemoryService:
    return MemoryService(db)


@pytest.fixture()
def embedder() -> HashEmbedder:
    return HashEmbedder()


@pytest.fixture()
def make_chat(db, settings, embedder):
    def factory(backend, with_docs: bool = True) -> ChatService:
        return ChatService(db, backend, settings, Retriever(db, embedder, settings) if with_docs else None)

    return factory


@pytest.fixture()
def doc_service(db, settings, embedder) -> DocumentService:
    return DocumentService(db, embedder, settings)


@pytest.fixture()
def task_service(db) -> TaskService:
    return TaskService(db)


@pytest.fixture()
def token() -> CancelToken:
    return CancelToken()
