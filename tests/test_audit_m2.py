"""Correções da auditoria independente da M2 (M2-A01 … M2-A06). Derivado dos casos de caracterização do Codex
(`M2-codex.md`), com as asserções invertidas para o comportamento correto."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QMessageBox

from nemec import documents
from nemec.backend import CancelToken, Delta, ToolCall
from nemec.documents import Passage, Retriever, plan_citations
from nemec.prompts import fit_passages
from nemec.services import ChatService
from nemec.ui.main_window import ImportWorker, MainWindow
from tests.fakes import ScriptedBackend


def tools(backend):
    return [t["function"]["name"] for t in backend.requests[0].tools]


class Orthogonal:
    def embed(self, texts):
        return [[0.0] * 1023 + [1.0] for _ in texts]


def mem(admin_conn):
    return [r["content"] for r in admin_conn.execute("SELECT content FROM memories ORDER BY content").fetchall()]


# ---- A01 ---------------------------------------------------------------------------------------
def test_a01_title_only_fact_never_becomes_memory(make_chat, doc_service, tmp_path, admin_conn):
    p = tmp_path / "Preferência de café da personagem fictícia — torrado.md"
    p.write_bytes(b"")
    assert doc_service.import_file(p).status == "no_text"
    backend = ScriptedBackend([Delta("Entendi."), ToolCall("save_memory", {"content": "Prefere café torrado"})])
    turn = make_chat(backend).run_turn(None, "Como se escreve café?", CancelToken())
    assert "torrado" in backend.requests[0].messages[0]["content"]  # o catálogo continua visível
    assert turn.status == "complete" and mem(admin_conn) == [] and turn.memory_refused


def test_a01_title_cannot_authorize_update_of_existing_memory(make_chat, doc_service, tmp_path, admin_conn):
    admin_conn.execute("INSERT INTO memories (content) VALUES ('Prefere chá')")
    (tmp_path / "Café torrado forte.md").write_bytes(b"")
    doc_service.import_file(tmp_path / "Café torrado forte.md")
    backend = ScriptedBackend([Delta("ok"), ToolCall("save_memory", {"content": "Prefere café torrado", "replaces": "m1"})])
    make_chat(backend).run_turn(None, "Como se escreve café?", CancelToken())
    assert mem(admin_conn) == ["Prefere chá"]


def test_a01_legitimate_user_statement_still_saved_with_library_present(make_chat, doc_service, tmp_path, admin_conn):
    (tmp_path / "Geometria.md").write_text("# G\n\ntriângulo", encoding="utf-8")
    doc_service.import_file(tmp_path / "Geometria.md")
    backend = ScriptedBackend([Delta("Anotado."), ToolCall("save_memory", {"content": "prefiro café torrado"})])
    turn = make_chat(backend).run_turn(None, "Eu prefiro café torrado.", CancelToken())
    assert mem(admin_conn) == ["prefiro café torrado"] and len(turn.memory_events) == 1


# ---- A02 ---------------------------------------------------------------------------------------
def test_a02_unrelated_chat_keeps_memory_with_ready_library(db, settings, doc_service, tmp_path, admin_conn):
    (tmp_path / "geometria.md").write_text("# Geometria\n\nUm triângulo isósceles tem dois lados iguais.", encoding="utf-8")
    assert doc_service.import_file(tmp_path / "geometria.md").status == "ready"
    backend = ScriptedBackend([Delta("Entendi."), ToolCall("save_memory", {"content": "Prefiro preparar café pela manhã"})])
    chat = ChatService(db, backend, settings, Retriever(db, Orthogonal(), settings))
    turn = chat.run_turn(None, "Prefiro preparar café pela manhã.", CancelToken())
    system = backend.requests[0].messages[0]["content"]
    assert "<trechos_recuperados>\n" not in system and "save_memory" in tools(backend)
    assert len(turn.memory_events) == 1 and mem(admin_conn) == ["Prefiro preparar café pela manhã"]


@pytest.mark.parametrize("q", ["quais arquivos você tem?", "o que falam esses documentos?", "resuma meus materiais"])
def test_a02_generic_library_questions_still_get_overview(db, settings, doc_service, tmp_path, q):
    (tmp_path / "geometria.md").write_text("# Geometria\n\nUm triângulo isósceles tem dois lados iguais.", encoding="utf-8")
    doc_service.import_file(tmp_path / "geometria.md")
    backend = ScriptedBackend([Delta("ok")])
    ChatService(db, backend, settings, Retriever(db, Orthogonal(), settings)).run_turn(None, q, CancelToken())
    assert "triângulo isósceles" in backend.requests[0].messages[0]["content"]


# ---- A06 ---------------------------------------------------------------------------------------
def test_a06_replace_never_duplicates_another_memory(make_chat, admin_conn):
    admin_conn.execute("INSERT INTO memories (content, created_at) VALUES ('Prefere chá', now() - interval '1 day'), ('prefiro café', now())")
    backend = ScriptedBackend([Delta("Entendi."), ToolCall("save_memory", {"content": "prefiro café", "replaces": "m1"})])
    turn = make_chat(backend).run_turn(None, "Troque a preferência de chá: agora prefiro café.", CancelToken())
    assert mem(admin_conn) == ["Prefere chá", "prefiro café"]  # nada duplicado nem sobrescrito
    assert turn.memory_events == []


# ---- A03 ---------------------------------------------------------------------------------------
def test_a03_directories_count_against_scan_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "FOLDER_MAX_VISITED", 50)
    for n in range(51):
        (tmp_path / f"d{n:05}").mkdir()
    scan = documents.scan_folder(tmp_path)
    assert "parou" in scan.note and scan.count == 0


def test_a03_batch_byte_bound_enforced_on_actual_reads(tmp_path, monkeypatch, doc_service, admin_conn):
    monkeypatch.setattr(documents, "FOLDER_MAX_BYTES", 100)
    files = [tmp_path / "a.md", tmp_path / "b.md", tmp_path / "c.md"]
    for f in files:
        f.write_text("Caderno curto")
    scan = documents.scan_folder(tmp_path)
    assert scan.count == 3 and scan.total_bytes < 100
    files[0].write_text("Geometria " * 8)  # 80 B: cabe
    files[1].write_text("Aritmética " * 8)  # >88 B: excede o que resta
    out = doc_service.import_files(scan.eligible, root=scan.root)
    # b excede o saldo: nada dele é lido (nem debitado); c ainda cabe nos 20 B restantes
    assert [o.status for o in out] == ["ready", "failed", "ready"] and out[1].bytes_read == 0
    assert "excede" in out[1].message
    assert sum(o.bytes_read for o in out) <= 100 + 1
    assert admin_conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 2  # a e c


def test_a03_file_count_limit_during_import(tmp_path, monkeypatch, doc_service):
    monkeypatch.setattr(documents, "FOLDER_MAX_FILES", 2)
    for n in range(4):
        (tmp_path / f"n{n}.md").write_text(f"# N{n}\n\ntexto {n}")
    out = doc_service.import_files(sorted(tmp_path.glob("*.md")), root=tmp_path)
    assert [o.status for o in out] == ["ready", "ready", "skipped", "skipped"]


# ---- A04 ---------------------------------------------------------------------------------------
def test_a04_cyclic_link_after_preview_does_not_stop_batch(doc_service, tmp_path, admin_conn):
    files = [tmp_path / f"{ch}.md" for ch in ("a", "b", "c")]
    for path in files:
        path.write_text(f"Conteúdo sintético de {path.name}", encoding="utf-8")
    scan = documents.scan_folder(tmp_path)
    files[1].unlink()
    files[1].symlink_to(files[1].name)  # laço de links criado depois da prévia
    worker = ImportWorker(doc_service, scan.eligible, scan.root)
    final = []
    worker.finished.connect(final.extend)
    worker.run()
    assert [r.status for r in final] == ["ready", "failed", "ready"] and final[1].message
    assert sorted(r["file_name"] for r in admin_conn.execute("SELECT file_name FROM documents").fetchall()) == ["a.md", "c.md"]


def test_a04_unexpected_exception_in_one_item_is_recorded(doc_service, tmp_path, monkeypatch):
    for n in "ab":
        (tmp_path / f"{n}.md").write_text(f"# {n}\n\ntexto {n}")
    real = doc_service.import_bytes
    monkeypatch.setattr(doc_service, "import_bytes", lambda name, *a: (_ for _ in ()).throw(RuntimeError("boom")) if name == "a.md" else real(name, *a))
    out = doc_service.import_files(sorted(tmp_path.glob("*.md")), root=tmp_path)
    assert [o.status for o in out] == ["failed", "ready"] and "boom" in out[0].message


# ---- A05 ---------------------------------------------------------------------------------------
def test_a05_adjacent_quote_only_literal_not_in_chunk_gets_no_highlight():
    p = Passage(1, uuid4(), uuid4(), "Aula", "aula.md", "md", None, "Seção", 0, "O triângulo isósceles tem dois lados iguais.")
    text = "“O triângulo isósceles tem dois lados iguais.” A resposta é “quadrado de quatro lados” [1]."
    cleaned, plans = plan_citations(text, [p])
    assert len(plans) == 1 and plans[0].quote_start is None and plans[0].quote_end is None  # citação válida, sem destaque
    _, ok = plan_citations("Como diz “O triângulo isósceles tem” [1].", [p])
    assert p.content[ok[0].quote_start : ok[0].quote_end] == "O triângulo isósceles tem"


def test_a05_budget_omitted_passage_is_not_citable(db, settings, doc_service, embedder, tmp_path, admin_conn):
    for n in range(8):
        path = tmp_path / f"texto {n}.md"
        path.write_text(str(n) + " " + "Geometria " * 119, encoding="utf-8")
        assert doc_service.import_file(path).status == "ready"
    backend = ScriptedBackend([Delta("A fonte da afirmação é [8] e também [1].")])
    chat = ChatService(db, backend, settings, Retriever(db, embedder, settings))
    turn = chat.run_turn(None, "Quais documentos?", CancelToken())
    prompt = backend.requests[0].messages[0]["content"]
    assert "[1] (" in prompt and "[8] (" not in prompt
    assert admin_conn.execute("SELECT ordinal FROM message_citations").fetchall() == [{"ordinal": 1}]
    assert turn.citations == 1 and "[8]" not in turn.text


def test_a05_fit_passages_matches_prompt():
    ps = [Passage(i, uuid4(), uuid4(), "t", f"f{i}.md", "md", None, None, 0, "x" * 1000) for i in range(1, 9)]
    fitted = fit_passages(ps, 6000)
    assert 0 < len(fitted) < 8 and [p.n for p in fitted] == list(range(1, len(fitted) + 1))


# ---- botão + -----------------------------------------------------------------------------------
def test_plus_button_offers_files_and_folder(qtbot, make_chat, memory_service, doc_service, task_service, admin_conn, tmp_path, monkeypatch):
    w = MainWindow(make_chat(ScriptedBackend([Delta("ok")])), memory_service, "m",
                   QSettings(str(tmp_path / "p.ini"), QSettings.IniFormat), documents=doc_service, tasks=task_service)
    qtbot.addWidget(w)
    assert w.attach_btn.isEnabled() and w.attach_btn.menu() is w.attach_menu
    assert [a.text() for a in w.attach_menu.actions()] == ["Importar arquivos…", "Importar pasta…"]
    from PySide6.QtWidgets import QFileDialog

    vault = tmp_path / "v"
    vault.mkdir()
    (vault / "n.md").write_text("# N\n\ntexto")
    f = tmp_path / "f.md"
    f.write_text("# F\n\ntexto f")
    w2 = MainWindow(make_chat(ScriptedBackend([Delta("ok")])), memory_service, "m",
                    QSettings(str(tmp_path / "p2.ini"), QSettings.IniFormat), documents=doc_service, tasks=task_service)
    qtbot.addWidget(w2)
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: ([str(f)], "")))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(vault)))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    acts = {a.text(): a for a in w2.attach_menu.actions()}
    acts["Importar arquivos…"].trigger()
    qtbot.waitUntil(lambda: w2._import_thread is None and w2.doc_list.count() == 1, timeout=10000)
    acts["Importar pasta…"].trigger()  # mesma prévia, confirmação e serviço da seção Documentos
    qtbot.waitUntil(lambda: w2._import_thread is None and w2.doc_list.count() == 2, timeout=10000)


# ---- Revalidação: A01 (origem literal) e A03 (débito de bytes) ---------------------------------
from nemec.backend import ToolCall as _TC  # noqa: E402
from tests.fakes import SequenceBackend  # noqa: E402

TITLE = "Preferência da personagem por filmes em preto e branco"


@pytest.mark.parametrize("replace", [False, True], ids=["criar", "atualizar"])
def test_a01_paraphrase_of_catalog_fact_is_refused(make_chat, doc_service, tmp_path, admin_conn, replace):
    path = tmp_path / f"{TITLE}.md"
    path.write_bytes(b"")
    assert doc_service.import_file(path).status == "no_text"
    args = {"content": "Prefere cinema monocromático"}
    if replace:
        admin_conn.execute("INSERT INTO memories(content) VALUES ('Prefere café')")
        args["replaces"] = "m1"
    backend = ScriptedBackend([Delta("Entendi."), ToolCall("save_memory", args)])
    turn = make_chat(backend).run_turn(None, "Como se escreve cinema?", CancelToken())
    assert TITLE in backend.requests[0].messages[0]["content"]  # o catálogo continua visível
    assert turn.memory_events == [] and turn.memory_refused and "literal" in turn.memory_refused[0]
    assert mem(admin_conn) == (["Prefere café"] if replace else [])


def test_a01_fact_from_assistant_history_is_refused(db, settings, doc_service, tmp_path, admin_conn):
    path = tmp_path / "Notas de leitura.md"
    excerpt = "A personagem prefere filmes em preto e branco."
    path.write_text(f"# Seção\n\n{excerpt}", encoding="utf-8")
    assert doc_service.import_file(path).status == "ready"
    backend = SequenceBackend(
        [Delta(f"{excerpt} [1]"), ToolCall("save_memory", {"content": "Prefere filmes em preto e branco"})],
        [Delta("Entendi."), ToolCall("save_memory", {"content": "Prefere cinema monocromático"}),
         ToolCall("save_memory", {"content": "A personagem prefere filmes em preto e branco"})],
    )
    chat = ChatService(db, backend, settings, Retriever(db, Orthogonal(), settings))
    first = chat.run_turn(None, "Resuma meus documentos e filmes.", CancelToken())
    assert first.citations == 1 and mem(admin_conn) == []
    second = chat.run_turn(first.conversation_id, "Como se escreve cinema?", CancelToken())
    assert any(m["role"] == "assistant" and excerpt in m["content"] for m in backend.requests[1].messages)  # está no histórico
    assert second.status == "complete" and mem(admin_conn) == [] and len(second.memory_refused) == 2


def test_a01_persisted_text_is_the_users_literal_excerpt(make_chat, doc_service, tmp_path, admin_conn):
    (tmp_path / "Geometria.md").write_text("# G\n\ntriângulo", encoding="utf-8")
    doc_service.import_file(tmp_path / "Geometria.md")
    backend = ScriptedBackend([Delta("Anotado."), ToolCall("save_memory", {"content": "MEU NOME É ana, "})])
    make_chat(backend).run_turn(None, "Oi! Meu nome é Ana. Pode me ajudar?", CancelToken())
    assert mem(admin_conn) == ["Meu nome é Ana"]  # o que se grava é o trecho exato da mensagem, não o texto do modelo


def test_a03_failed_indexing_still_debits_bytes(doc_service, tmp_path, monkeypatch, admin_conn):
    monkeypatch.setattr(documents, "FOLDER_MAX_BYTES", 100)
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text("Caderno curto")
    b.write_text("Caderno breve")
    scan = documents.scan_folder(tmp_path)
    a.write_text("Geometria " * 8)  # 80 B
    b.write_text("Aritmetica " * 7)  # 77 B: só restam 20
    real = doc_service.import_bytes

    def index(name, kind, data):
        if name == a.name:
            raise RuntimeError("falha transitória na indexação")
        return real(name, kind, data)

    monkeypatch.setattr(doc_service, "import_bytes", index)
    results = doc_service.import_files(scan.eligible, root=scan.root)
    assert [r.status for r in results] == ["failed", "failed"] and results[0].bytes_read == 80
    assert "excede" in results[1].message and sum(r.bytes_read for r in results) <= 100
    assert admin_conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 0


# ---- Revalidação final: A01 (atribuição ao usuário) e A03 (leitura limitada ao saldo) -----------
from nemec import memory_policy as _p  # noqa: E402


@pytest.mark.parametrize("replace", [False, True], ids=["criar", "atualizar"])
def test_a01_third_party_quote_in_current_message_is_refused(make_chat, doc_service, tmp_path, admin_conn, replace):
    path = tmp_path / f"{TITLE}.md"
    path.write_bytes(b"")
    doc_service.import_file(path)
    fact = "Eu prefiro cinema monocromático"
    args = {"content": fact}
    if replace:
        admin_conn.execute("INSERT INTO memories(content) VALUES ('Eu prefiro café')")
        args["replaces"] = "m1"
    backend = ScriptedBackend([Delta("Entendi."), ToolCall("save_memory", args)])
    turn = make_chat(backend).run_turn(None, f"A personagem do título diz «{fact}». Isso é a preferência dela?", CancelToken())
    assert turn.memory_events == [] and turn.memory_refused
    assert mem(admin_conn) == (["Eu prefiro café"] if replace else [])


@pytest.mark.parametrize("msg,span", [
    ('A personagem diz "Eu prefiro cinema mudo".', "Eu prefiro cinema mudo"),   # aspas
    ("Segundo o documento, eu prefiro cinema mudo.", "eu prefiro cinema mudo"),  # atribuição na frase
    ("Ela diz: prefiro cinema mudo.", "prefiro cinema mudo"),                    # terceiro + dois-pontos
    ("O texto afirma que moro em Marte.", "moro em Marte"),
    ("cinema mudo é legal", "cinema mudo é legal"),                              # sem marca de 1ª pessoa
    ("Minha vizinha gostou de cinema mudo.", "gostou de cinema mudo"),           # -ou = 3ª pessoa do pretérito
    ("Meu irmão adorou cinema mudo.", "adorou cinema mudo"),
])
def test_a01_attribution_rules_refuse(msg, span):
    assert _p.literal_span(span, msg) is None


@pytest.mark.parametrize("msg,span,expected", [
    ("Meu nome é Lucas e eu moro em Campinas.", "eu moro em Campinas", "eu moro em Campinas"),
    ("Prefiro respostas curtas, de no máximo duas frases.", "Prefiro respostas curtas", "Prefiro respostas curtas"),
    ("Na verdade, moro em Recife agora.", "Na verdade, moro em Recife agora", "Na verdade, moro em Recife agora"),
    ("Gosto de café sem açúcar. A personagem diz «gosto de chá».", "Gosto de café sem açúcar", "Gosto de café sem açúcar"),
])
def test_a01_clear_first_person_statements_accepted(msg, span, expected):
    assert _p.literal_span(span, msg) == expected


def test_a03_reads_never_exceed_balance_even_with_buffering(doc_service, tmp_path, monkeypatch, admin_conn):
    import builtins

    monkeypatch.setattr(documents, "FOLDER_MAX_BYTES", 100)
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text("Caderno curto")
    b.write_text("Caderno breve")
    scan = documents.scan_folder(tmp_path)
    a.write_text("Geometria " * 8)  # 80 B
    b.write_text("Aritmetica " * 7)  # 77 B; saldo 20
    consumed = []
    real_open = builtins.open

    def spy(path, mode="r", *args, **kw):
        fh = real_open(path, mode, *args, **kw)
        if str(path).endswith(".md") and "b" in mode:
            raw = fh
            orig = raw.read

            def read(n=-1):
                d = orig(n)
                consumed.append((str(path).rsplit("/", 1)[-1], len(d)))
                return d

            raw.read = read  # type: ignore[method-assign]
        return fh

    monkeypatch.setattr(documents, "open", spy, raising=False)
    monkeypatch.setattr(doc_service, "import_bytes", lambda *a: (_ for _ in ()).throw(RuntimeError("falha na indexação")))
    results = doc_service.import_files(scan.eligible, root=scan.root)
    assert [r.status for r in results] == ["failed", "failed"] and results[0].bytes_read == 80
    assert sum(n for name, n in consumed) <= 100 and not any(name == "b.md" for name, _ in consumed)  # b nem foi lido
    # a leitura de a foi debitada ANTES de indexar: o saldo que b viu já era 20
    assert "excede" in results[1].message


def test_a03_growth_during_read_is_detected(doc_service, tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "FOLDER_MAX_BYTES", 50)
    f = tmp_path / "a.md"
    f.write_text("x" * 40)
    scan = documents.scan_folder(tmp_path)
    f.write_text("x" * 60)
    r = doc_service.import_files(scan.eligible, root=scan.root)[0]
    assert r.status == "failed" and "excede" in r.message and r.bytes_read == 0


# ---- A01 final: possessivo não prova autoria ---------------------------------------------------
THIRD = [("Minha vizinha gostou de cinema mudo.", "gostou de cinema mudo"), ("Meu irmão adorou cinema mudo.", "adorou cinema mudo")]
THIRD_FULL = [(m, m) for m, _ in THIRD] + [("Meu irmão gosto de cinema mudo.", "Meu irmão gosto de cinema mudo")]


@pytest.mark.parametrize("msg,sel", THIRD + THIRD_FULL)
@pytest.mark.parametrize("replace", [False, True], ids=["criar", "substituir"])
def test_a01_third_party_possessive_statements_never_persist(make_chat, admin_conn, msg, sel, replace):
    args = {"content": sel}
    if replace:
        admin_conn.execute("INSERT INTO memories(content) VALUES ('Prefiro cinema falado')")
        args["replaces"] = "m1"
    turn = make_chat(ScriptedBackend([Delta("Entendi."), ToolCall("save_memory", args)])).run_turn(None, msg, CancelToken())
    assert turn.memory_events == [] and turn.memory_refused
    assert mem(admin_conn) == (["Prefiro cinema falado"] if replace else [])


@pytest.mark.parametrize("msg,sel", [
    ("Eu prefiro café.", "Eu prefiro café"), ("Moro em Recife.", "Moro em Recife"), ("Gosto de estudar.", "Gosto de estudar"),
    ("Meu nome é Lucas.", "Meu nome é Lucas"), ("Minha cidade é Recife.", "Minha cidade é Recife"),
    ("Meu filme favorito é Metrópolis.", "Meu filme favorito é Metrópolis"),
])
def test_a01_legitimate_personal_statements_still_create(make_chat, admin_conn, msg, sel):
    turn = make_chat(ScriptedBackend([Delta("Anotado."), ToolCall("save_memory", {"content": sel})])).run_turn(None, msg, CancelToken())
    assert [e["action"] for e in turn.memory_events] == ["created"] and mem(admin_conn) == [sel.rstrip(".")]


def test_a01_legitimate_personal_statement_still_updates(make_chat, admin_conn):
    admin_conn.execute("INSERT INTO memories(content) VALUES ('Moro em Campinas')")
    turn = make_chat(ScriptedBackend([Delta("ok"), ToolCall("save_memory", {"content": "Moro em Recife", "replaces": "m1"})])).run_turn(
        None, "Mudei: Moro em Recife.", CancelToken())
    assert [e["action"] for e in turn.memory_events] == ["updated"] and mem(admin_conn) == ["Moro em Recife"]
