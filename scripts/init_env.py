"""Cria .env a partir de .env.example com senhas aleatórias (não sobrescreve um .env existente)."""
import os
import secrets
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
target, example = root / ".env", root / ".env.example"
if target.exists():
    print(".env já existe; mantido.")
    sys.exit(0)
pg, app = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
text = example.read_text(encoding="utf-8")
text = text.replace("NEMEC_POSTGRES_PASSWORD=__GERAR__", f"NEMEC_POSTGRES_PASSWORD={pg}")
text = text.replace("NEMEC_APP_PASSWORD=__GERAR__", f"NEMEC_APP_PASSWORD={app}")
text = text.replace("__NEMEC_POSTGRES_PASSWORD__", pg).replace("__NEMEC_APP_PASSWORD__", app)
port = os.environ.get("NEMEC_DB_PORT", "5433")  # permite instalar ao lado de outra instância
if not port.isdigit():
    sys.exit(f"NEMEC_DB_PORT inválida: {port!r}")
text = text.replace("NEMEC_DB_PORT=5433", f"NEMEC_DB_PORT={port}").replace("__NEMEC_DB_PORT__", port)
target.write_text(text, encoding="utf-8")
target.chmod(0o600)
print(".env criado (permissão 600).")
