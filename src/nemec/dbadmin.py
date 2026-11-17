"""Administração do esquema: `python -m nemec.dbadmin migrate`.

Conecta com a URL administrativa (superusuário do contêiner) apenas para executar as migrações
como `nemec_owner` (SET ROLE). A aplicação nunca usa essas credenciais.
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from nemec import config as cfg


def alembic_config(url: str) -> Config:
    sa_url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    conf = Config()
    conf.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    conf.attributes["admin_url"] = sa_url
    return conf


def migrate(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["migrate"]:
        print("uso: python -m nemec.dbadmin migrate", file=sys.stderr)
        return 2
    cfg.load_dotenv(cfg.project_root() / ".env")
    migrate(cfg.admin_url())
    print("migrações aplicadas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
