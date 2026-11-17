"""Ponto de entrada: `python -m nemec` / `make run`."""
from __future__ import annotations

import sys

from nemec.backend import OllamaBackend, OllamaEmbedder
from nemec.config import load_settings
from nemec.db import Database
from nemec.errors import ConfigError
from nemec.config import EMBED_DIM
from nemec.documents import DocumentService, Retriever
from nemec.services import ChatService, MemoryService, TaskService


def build_services(settings=None):
    settings = settings or load_settings()
    db = Database(settings.database_url)
    embedder = OllamaEmbedder(settings.ollama_host, settings.embed_model, EMBED_DIM)
    chat = ChatService(db, OllamaBackend(settings.ollama_host), settings, Retriever(db, embedder, settings))
    return settings, chat, MemoryService(db), DocumentService(db, embedder, settings), TaskService(db)


def main() -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    app.setStyle("Fusion")  # aparência consistente com o QSS do tema
    try:
        settings, chat, memories, documents, tasks = build_services()
        chat.recover_orphans()  # `generating` órfão → `failed`
        documents.recover_orphans()  # `processing` órfão → `failed`
    except ConfigError as exc:
        QMessageBox.critical(None, "Nemec", f"Configuração inválida:\n{exc}")
        return 2
    except Exception as exc:
        QMessageBox.critical(None, "Nemec", f"Não foi possível acessar o banco (make db-up / make migrate?):\n{exc}")
        return 2
    from nemec.ui.main_window import MainWindow

    win = MainWindow(chat, memories, settings.chat_model, documents=documents, tasks=tasks)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
