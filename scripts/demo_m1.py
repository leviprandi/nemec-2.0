"""Demonstração roteirizada de M1 com o Ollama real (banco temporário, descartado ao final).

Saídas: demo-output/demo-m1.md e capturas PNG da interface (offscreen).
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import psycopg  # noqa: E402

from nemec import config as cfg  # noqa: E402
from nemec import dbadmin  # noqa: E402
from nemec.backend import CancelToken, OllamaBackend  # noqa: E402
from nemec.config import Settings  # noqa: E402
from nemec.db import Database  # noqa: E402
from nemec.services import ChatService, MemoryService  # noqa: E402

ROOT = cfg.project_root()
OUT = Path(os.environ["NEMEC_DEMO_OUT"]) if os.environ.get("NEMEC_DEMO_OUT") else ROOT / "demo-output"
log: list[str] = []


def say(line: str = "") -> None:
    print(line)
    log.append(line)


def with_db(url: str, name: str) -> str:
    return urlparse(url)._replace(path=f"/{name}").geturl()


def main() -> int:
    cfg.load_dotenv(ROOT / ".env")
    admin = cfg.admin_url()
    name = f"nemec_demo_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(with_db(admin, name), autocommit=True) as c:
            c.execute((ROOT / "db" / "bootstrap.sql").read_text())
        dbadmin.migrate(with_db(admin, name))
        settings = Settings(database_url=with_db(os.environ["NEMEC_DATABASE_URL"], name),
                            chat_model=os.environ.get("NEMEC_CHAT_MODEL", "qwen3.5:9b"),
                            ollama_host=os.environ.get("NEMEC_OLLAMA_HOST", "http://127.0.0.1:11434"))
        return run(settings)
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "demo-m1.md").write_text("# Demonstração M1 (Ollama real)\n\n```\n" + "\n".join(log) + "\n```\n", encoding="utf-8")


def run(settings: Settings) -> int:
    db = Database(settings.database_url)
    chat = ChatService(db, OllamaBackend(settings.ollama_host), settings)
    mem = MemoryService(db)
    say(f"Modelo: {settings.chat_model} | Ollama: {settings.ollama_host} | data: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    say("\n[1] Memória automática: o usuário diz um fato; nenhuma confirmação é pedida")
    chunks: list[str] = []
    t0 = time.monotonic()
    r1 = chat.run_turn(None, "Meu nome é Lucas e eu prefiro respostas curtas, de no máximo duas frases.",
                       CancelToken(), chunks.append)
    say(f"status={r1.status} chunks={len(chunks)} tempo={time.monotonic() - t0:.1f}s")
    say(f"resposta: {r1.text!r}")
    say(f"eventos de memória: {[(e['action'], e['content']) for e in r1.memory_events]} | recusadas: {r1.memory_refused} | rejeitadas: {r1.rejected_tool_calls}")
    say(f"memórias: {[m['content'] for m in mem.list_memories()]} | propostas pendentes: {len(mem.list_pending(r1.conversation_id))} (esperado 0)")
    say(f"resposta não vazia: {bool(r1.text.strip())}")

    say("\n[2] Atualização e prevenção de duplicidade")
    r2a = chat.run_turn(r1.conversation_id, "Na verdade, prefiro respostas de um parágrafo, não duas frases.", CancelToken())
    say(f"atualização: {[(e['action'], e['content'], e['previous']) for e in r2a.memory_events]} | resposta: {r2a.text!r}")
    r2b = chat.run_turn(r1.conversation_id, "Lembre-se: meu nome é Lucas.", CancelToken())
    say(f"repetição: eventos={[(e['action'], e['content']) for e in r2b.memory_events]} | resposta: {r2b.text!r}")
    say(f"memórias agora: {[m['content'] for m in mem.list_memories()]}")

    say("\n[3] Segredo não é guardado; memória aparece no prompt de uma nova conversa")
    rs = chat.run_turn(r1.conversation_id, "Guarde isso: a senha do meu wifi é Abc12345xyz.", CancelToken())
    say(f"segredo: eventos={rs.memory_events} recusadas={rs.memory_refused} | resposta: {rs.text!r}")
    say(f"memórias contêm 'Abc12345': {any('Abc12345' in m['content'] for m in mem.list_memories())} (esperado False)")
    r2 = chat.run_turn(None, "Qual é o meu nome e como você deve responder para mim?", CancelToken())
    say(f"status={r2.status} resposta: {r2.text!r}")

    say("\n[4] Cancelamento no meio do stream")
    token, got, out = CancelToken(), threading.Event(), {}
    th = threading.Thread(target=lambda: out.setdefault("r", chat.run_turn(
        r2.conversation_id, "Escreva um texto longo de 1500 palavras sobre a história do café.", token,
        lambda _t: got.set())))
    th.start()
    got.wait(120)
    time.sleep(1.0)
    c0 = time.monotonic()
    token.cancel()
    th.join(15)
    r3 = out["r"]
    say(f"status={r3.status} texto parcial={len(r3.text)} caracteres | latência do cancelamento={time.monotonic() - c0:.2f}s")

    say("\n[5] Histórico persistente")
    msgs = chat.get_messages(r2.conversation_id)
    say("conversa reaberta: " + ", ".join(f"{m['seq']}:{m['role']}:{m['status']}" for m in msgs))
    say(f"conversas: {[c['title'] for c in chat.list_conversations()]}")

    say("\n[6] Excluir conversa mantém memórias")
    chat.delete_conversation(r1.conversation_id)
    say(f"conversas restantes={len(chat.list_conversations())} memórias={len(mem.list_memories())}")

    say("\n[7] Capturas da interface (offscreen, tema claro e escuro)")
    shot(chat, mem, settings)
    say("OK: demo-output/ui-*.png")
    return 0


def shot(chat, mem, settings) -> None:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from nemec.ui import theme
    from nemec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    import tempfile

    prefs = QSettings(str(Path(tempfile.mkdtemp()) / "demo.ini"), QSettings.IniFormat)
    w = MainWindow(chat, mem, settings.chat_model, prefs)
    w.resize(1240, 780)
    w.show()
    w.input.setPlainText("Eu moro em Campinas e gosto de café sem açúcar. Pode me sugerir um roteiro de manhã bem curto?")
    w.send_btn.click()
    deadline = time.monotonic() + 180
    while w.busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    OUT.mkdir(parents=True, exist_ok=True)
    w.grab().save(str(OUT / "ui-claro.png"))
    theme_orig = theme.is_dark
    theme.is_dark = lambda _app: True
    w._apply_theme()
    app.processEvents()
    w.grab().save(str(OUT / "ui-escuro.png"))
    w.resize(700, 700)
    app.processEvents()
    w.grab().save(str(OUT / "ui-estreito.png"))
    theme.is_dark = theme_orig
    say(f"UI: conversas={w.conv_list.count()} memórias listadas={w.mem_list.count()} cartões pendentes={w.proposals_box.count()}")
    w.close()


if __name__ == "__main__":
    raise SystemExit(main())
