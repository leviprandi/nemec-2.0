"""Integração com o Ollama local real (modelo padrão). Excluir com: pytest -m "not real_ollama"."""
from __future__ import annotations

import threading
import time

import pytest

from nemec.backend import OllamaBackend
from nemec.config import Settings
from nemec.services import ChatService

pytestmark = pytest.mark.real_ollama


@pytest.fixture()
def real_chat(db, test_app_url, env):
    s = Settings(database_url=test_app_url, chat_model=env.get("NEMEC_CHAT_MODEL", "qwen3.5:9b"),
                 ollama_host=env.get("NEMEC_OLLAMA_HOST", "http://127.0.0.1:11434"))
    return ChatService(db, OllamaBackend(s.ollama_host), s)


def test_real_streaming_complete(real_chat, token, admin_conn):
    chunks = []
    r = real_chat.run_turn(None, "Responda apenas com a palavra: pronto", token, chunks.append)
    assert r.status == "complete", r.error
    assert len(chunks) >= 1 and "".join(chunks).strip() == r.text and r.text.strip()


def test_real_cancel_interrupts_stream_quickly(real_chat, token, admin_conn):
    got = threading.Event()
    out = {}

    def run():
        out["r"] = real_chat.run_turn(None, "Escreva um texto muito longo, com 2000 palavras, sobre a história do café.",
                                      token, lambda _t: got.set())

    t = threading.Thread(target=run)
    t.start()
    assert got.wait(120), "nenhum token recebido"
    t0 = time.monotonic()
    token.cancel()
    t.join(15)
    elapsed = time.monotonic() - t0
    assert not t.is_alive() and elapsed < 5, f"cancelamento demorou {elapsed:.1f}s"
    r = out["r"]
    assert r.status == "canceled" and r.text
    row = admin_conn.execute("SELECT status, content FROM messages WHERE role='assistant'").fetchone()
    assert row["status"] == "canceled" and row["content"] == r.text


def test_real_cancel_before_first_token(real_chat, token, admin_conn):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", real_chat.run_turn(None, "Explique relatividade em detalhes.", token)))
    t.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    token.cancel()
    t.join(15)
    assert not t.is_alive() and time.monotonic() - t0 < 5
    assert out["r"].status in ("canceled", "complete")


# ---- M2: embeddings reais (bge-m3) e RAG ponta a ponta ------------------------------------------
@pytest.fixture()
def real_rag(db, test_app_url, env, tmp_path):
    from nemec.backend import OllamaEmbedder
    from nemec.documents import DocumentService, Retriever

    s = Settings(database_url=test_app_url, chat_model=env.get("NEMEC_CHAT_MODEL", "qwen3.5:9b"),
                 embed_model=env.get("NEMEC_EMBED_MODEL", "bge-m3"),
                 ollama_host=env.get("NEMEC_OLLAMA_HOST", "http://127.0.0.1:11434"))
    emb = OllamaEmbedder(s.ollama_host, s.embed_model)
    return ChatService(db, OllamaBackend(s.ollama_host), s, Retriever(db, emb, s)), DocumentService(db, emb, s), tmp_path


def test_real_embeddings_have_1024_dims_and_semantic_order(env):
    from nemec.backend import OllamaEmbedder

    emb = OllamaEmbedder(env.get("NEMEC_OLLAMA_HOST", "http://127.0.0.1:11434"), env.get("NEMEC_EMBED_MODEL", "bge-m3"))
    q, a, b = emb.embed(["Como as plantas produzem energia?", "A fotossíntese converte luz em energia química nas plantas.",
                         "O Banco Central define a taxa Selic."])
    assert len(q) == 1024
    dot = lambda x, y: sum(i * j for i, j in zip(x, y))
    assert dot(q, a) > dot(q, b)


def test_real_rag_end_to_end_with_citation(real_rag, token, admin_conn):
    chat, docs, tmp = real_rag
    f = tmp / "biologia.md"
    f.write_text("# Biologia\n\n## Fotossíntese\n\nA fotossíntese é o processo pelo qual as plantas convertem luz solar, "
                 "água e gás carbônico em glicose e oxigênio. Ocorre nos cloroplastos.\n\n## Célula\n\n"
                 "A mitocôndria é a organela responsável pela respiração celular e pela produção de ATP.\n", encoding="utf-8")
    assert docs.import_file(f).status == "ready"
    r = chat.run_turn(None, "Onde ocorre a fotossíntese, segundo meu material? Cite a fonte.", token)
    assert r.status == "complete", r.error
    assert "cloroplasto" in r.text.lower() and r.citations >= 1, r.text
    c = admin_conn.execute("SELECT c.section, c.page FROM message_citations mc JOIN document_chunks c ON c.id = mc.chunk_id").fetchone()
    assert c["page"] is None and "Fotossíntese" in c["section"]


def test_real_long_text_request_overrides_brief_memory_preference(real_chat, token, admin_conn):
    admin_conn.execute("INSERT INTO memories (content) VALUES ('Prefere respostas objetivas e curtas')")
    r = real_chat.run_turn(None, "Escreva um texto longo (pelo menos 250 palavras) sobre a importância de dormir bem.", token)
    assert r.status == "complete", r.error
    assert len(r.text.split()) >= 200, r.text


def test_real_generic_question_recognizes_library(real_rag, token, admin_conn):
    chat, docs, tmp = real_rag
    (tmp / "Curriculum.md").write_text("# Currículo\n\nLucas Morel, desenvolvedor com experiência em Python, bancos de dados e IA.\n", encoding="utf-8")
    (tmp / "00 - Apostila AI Engineer (MOC).md").write_text("# Apostila AI Engineer\n\nRoteiro de estudos: embeddings, RAG, agentes e avaliação de modelos.\n", encoding="utf-8")
    assert docs.import_file(tmp / "Curriculum.md").status == "ready" and docs.import_file(tmp / "00 - Apostila AI Engineer (MOC).md").status == "ready"
    r = chat.run_turn(None, "o que falam esses arquivos?", token)
    low = r.text.lower()
    assert r.status == "complete", r.error
    assert "nenhum arquivo" not in low and "não há arquivos" not in low and "não foi enviado" not in low, r.text
    assert "curr" in low and ("apostila" in low or "ai engineer" in low), r.text  # descreve os DOIS arquivos
    r2 = chat.run_turn(None, "quais arquivos você tem?", token)
    assert "curr" in r2.text.lower() and "apostila" in r2.text.lower(), r2.text  # título pode vir traduzido


def test_real_memory_saves_literal_user_excerpt(real_chat, token, admin_conn):
    r = real_chat.run_turn(None, "Meu nome é Lucas e eu moro em Campinas.", token)
    assert r.status == "complete", r.error
    saved = [m["content"] for m in admin_conn.execute("SELECT content FROM memories")]
    assert saved and all(s in "Meu nome é Lucas e eu moro em Campinas." for s in saved), saved
