"""Interface PySide6 (offscreen, pytest-qt) com backend falso."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QInputDialog, QLabel, QMessageBox
from psycopg.types.json import Jsonb

from nemec.backend import Delta, ToolCall
from nemec.errors import BackendUnavailable
from nemec.ui.main_window import MainWindow, ProposalCard
from tests.fakes import GatedBackend, ScriptedBackend, SequenceBackend


@pytest.fixture()
def make_win(qtbot, make_chat, memory_service, doc_service, task_service, admin_conn, tmp_path):
    def factory(backend, prefs=None):
        prefs = prefs or QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat)
        w = MainWindow(make_chat(backend), memory_service, "modelo-teste", prefs, documents=doc_service, tasks=task_service)
        qtbot.addWidget(w)
        return w

    return factory


def send(qtbot, w, text):
    w.input.setPlainText(text)
    w.send_btn.click()
    qtbot.waitUntil(lambda: not w.busy, timeout=10000)


def notes(w):
    return [lbl.text() for lbl in w.view.findChildren(QLabel, "memoryNote")]


def test_chat_streaming_and_history(qtbot, make_win):
    w = make_win(ScriptedBackend([Delta("Olá "), Delta("mundo")]))
    send(qtbot, w, "oi")
    assert "Olá mundo" in w.view.plain_text() and "oi" in w.view.plain_text()
    assert w.conv_list.count() == 1 and w.conversation_id is not None
    assert w.title_label.text() == "oi"
    w.new_conversation()
    assert w.view.plain_text().strip() == "" and w.conversation_id is None and not w.view._welcome.isHidden()
    w.conv_list.setCurrentRow(0)  # reabre
    assert "Olá mundo" in w.view.plain_text()


def test_send_button_enabled_only_with_text(qtbot, make_win):
    w = make_win(ScriptedBackend([Delta("ok")]))
    assert not w.send_btn.isEnabled()
    w.input.setPlainText("  ")
    assert not w.send_btn.isEnabled()
    w.input.setPlainText("oi")
    assert w.send_btn.isEnabled()


def test_cancel_button_keeps_partial(qtbot, make_win, admin_conn):
    backend = GatedBackend("parcial")
    w = make_win(backend)
    w.input.setPlainText("conte")
    w.send_btn.click()
    qtbot.waitUntil(backend.started.is_set, timeout=5000)
    assert not w.cancel_btn.isHidden() and w.send_btn.isHidden()
    w.cancel_btn.click()
    qtbot.waitUntil(lambda: not w.busy, timeout=10000)
    assert "parcial" in w.view.plain_text() and "cancelada" in w.view.plain_text()
    assert admin_conn.execute("SELECT status FROM messages WHERE role='assistant'").fetchone()["status"] == "canceled"
    assert "nunca deve ser gravado" not in w.view.plain_text()
    assert not w.notice.isHidden()  # estado "cancelada" identificado


def test_loading_state_shows_thinking_placeholder(qtbot, make_win):
    backend = GatedBackend("")
    w = make_win(backend)
    w.input.setPlainText("oi")
    w.send_btn.click()
    assert "Pensando" in w.view.message_widgets[-1].body.text()
    backend.release.set()
    qtbot.waitUntil(lambda: not w.busy, timeout=10000)


def test_ollama_unavailable_shows_clear_error(qtbot, make_win, admin_conn):
    w = make_win(ScriptedBackend([BackendUnavailable("Não foi possível conectar ao Ollama em x.")]))
    send(qtbot, w, "oi")
    assert not w.banner.isHidden() and "Ollama" in w.banner.text()
    assert "falhou" in w.view.plain_text()
    assert admin_conn.execute("SELECT status FROM messages WHERE role='assistant'").fetchone()["status"] == "failed"


def test_model_text_is_escaped(qtbot, make_win):
    evil = "<b>negrito</b><img src='http://evil.example/x.png'><a href='http://evil.example'>clique</a>"
    w = make_win(ScriptedBackend([Delta(evil)]))
    send(qtbot, w, "oi")
    body = w.view.message_widgets[-1].body
    assert evil in w.view.plain_text()
    assert "<img" not in body.text() and "<a href" not in body.text() and "&lt;img" in body.text()
    assert not body.openExternalLinks() and not (body.textInteractionFlags() & Qt.LinksAccessibleByMouse)
    html = body.toolTip()  # sem tooltip/recurso do modelo
    assert html == ""


def test_basic_markdown_is_rendered_safely(qtbot, make_win):
    w = make_win(ScriptedBackend([Delta("Use **negrito** e `code`:\n\n- um\n- dois")]))
    send(qtbot, w, "oi")
    html = w.view.message_widgets[-1].body.text()
    assert "<b>negrito</b>" in html and "<code" in html and "•" in html


def test_auto_memory_saves_and_shows_discreet_note(qtbot, make_win, admin_conn):
    w = make_win(SequenceBackend([ToolCall("save_memory", {"content": "Meu nome é Lucas"})], [Delta("Prazer, Lucas!")]))
    send(qtbot, w, "Meu nome é Lucas")
    assert not w.findChildren(ProposalCard)
    assert w.mem_list.count() == 1 and w.mem_list.item(0).text() == "Meu nome é Lucas"
    assert "Prazer, Lucas!" in w.view.plain_text()  # nunca resposta vazia
    assert notes(w) == ["Memória guardada · Meu nome é Lucas"]
    assert not w.notice.isHidden() and "Memória guardada" in w.notice.text()
    w.new_conversation()
    w.conv_list.setCurrentRow(0)
    assert len(notes(w)) == 1  # a indicação sobrevive à reabertura


def test_memory_update_is_indicated(qtbot, make_win, admin_conn):
    admin_conn.execute("INSERT INTO memories (content) VALUES ('Mora em Recife')")
    w = make_win(ScriptedBackend([Delta("Anotado."), ToolCall("save_memory", {"content": "moro em São Paulo", "replaces": "m1"})]))
    w.refresh_memories()
    send(qtbot, w, "Agora moro em São Paulo")
    assert w.mem_list.count() == 1 and w.mem_list.item(0).text() == "moro em São Paulo"
    assert notes(w) == ["Memória atualizada · moro em São Paulo"] and "atualizada" in w.notice.text()


def test_secret_is_not_saved_and_user_is_told(qtbot, make_win, admin_conn):
    w = make_win(ScriptedBackend([Delta("Prefiro não guardar isso."), ToolCall("save_memory", {"content": "A senha do banco é abc123"})]))
    send(qtbot, w, "Guarde: a senha do banco é abc123")
    assert w.mem_list.count() == 0 and "Não guardei" in w.notice.text()


def test_auto_memory_toggle_off_persists_and_stops_saving(qtbot, make_win, admin_conn, tmp_path):
    prefs = QSettings(str(tmp_path / "p.ini"), QSettings.IniFormat)
    w = make_win(ScriptedBackend([Delta("ok"), ToolCall("save_memory", {"content": "eu gosto de jazz"})]), prefs)
    assert w.auto_mem_check.isChecked()
    w.auto_mem_check.setChecked(False)
    prefs.sync()
    send(qtbot, w, "Eu gosto de jazz")
    assert w.mem_list.count() == 0 and not notes(w)
    w2 = make_win(ScriptedBackend([Delta("ok")]), QSettings(str(tmp_path / "p.ini"), QSettings.IniFormat))
    assert not w2.auto_mem_check.isChecked()


@pytest.fixture()
def pending_card(admin_conn):
    cid = admin_conn.execute("INSERT INTO conversations (title) VALUES ('c') RETURNING id").fetchone()["id"]
    admin_conn.execute("INSERT INTO action_proposals (kind, arguments, conversation_id) VALUES ('create_memory', %s, %s)",
                       (Jsonb({"content": "eu gosto de jazz"}), cid))
    return cid


def test_pending_proposal_card_confirm_with_edit(qtbot, make_win, pending_card, admin_conn):
    w = make_win(ScriptedBackend([Delta("ok")]))
    w.open_conversation(pending_card)
    cards = w.findChildren(ProposalCard)
    assert len(cards) == 1 and w.mem_list.count() == 0
    cards[0].edit.setPlainText("Adora jazz")
    cards[0].confirm_btn.click()
    assert w.mem_list.count() == 1 and w.mem_list.item(0).text() == "Adora jazz" and not w.findChildren(ProposalCard)


def test_confirm_disabled_for_empty_text_and_reject(qtbot, make_win, pending_card, admin_conn):
    w = make_win(ScriptedBackend([Delta("ok")]))
    w.open_conversation(pending_card)
    card = w.findChildren(ProposalCard)[0]
    card.edit.setPlainText("   ")
    assert not card.confirm_btn.isEnabled()
    card.reject_btn.click()
    assert w.mem_list.count() == 0 and not w.findChildren(ProposalCard)
    assert admin_conn.execute("SELECT status FROM action_proposals").fetchone()["status"] == "rejected"


def test_edit_and_delete_memory_via_ui(qtbot, make_win, monkeypatch, admin_conn):
    admin_conn.execute("INSERT INTO memories (content) VALUES ('Gosta de jazz')")
    w = make_win(ScriptedBackend([Delta("ok")]))
    w.mem_list.setCurrentRow(0)
    monkeypatch.setattr(QInputDialog, "getMultiLineText", staticmethod(lambda *a, **k: ("Novo texto", True)))
    w.mem_edit_btn.click()
    assert w.mem_list.item(0).text() == "Novo texto"
    monkeypatch.setattr(QInputDialog, "getMultiLineText", staticmethod(lambda *a, **k: ("  ", True)))
    w.mem_list.setCurrentRow(0)
    w.mem_edit_btn.click()  # vazio: erro, nada muda
    assert not w.banner.isHidden() and w.mem_list.item(0).text() == "Novo texto"
    w.mem_list.setCurrentRow(0)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    w.mem_del_btn.click()
    assert w.mem_list.count() == 0 and "excluída" in w.notice.text()


def test_delete_conversation_keeps_memories(qtbot, make_win, monkeypatch, admin_conn):
    w = make_win(SequenceBackend([ToolCall("save_memory", {"content": "eu gosto de jazz"})], [Delta("legal")]))
    send(qtbot, w, "eu gosto de jazz")
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    w.delete_conversation()
    assert w.conv_list.count() == 0 and w.mem_list.count() == 1
    assert admin_conn.execute("SELECT count(*) AS n FROM messages").fetchone()["n"] == 0


def test_enter_sends_and_shift_enter_breaks_line(qtbot, make_win):
    w = make_win(ScriptedBackend([Delta("ok")]))
    w.input.setPlainText("linha1")
    qtbot.keyClick(w.input, Qt.Key_Return, Qt.ShiftModifier)
    assert not w.busy and "\n" in w.input.toPlainText()
    qtbot.keyClick(w.input, Qt.Key_Return)
    qtbot.waitUntil(lambda: not w.busy and w.conversation_id is not None, timeout=10000)
    assert "ok" in w.view.plain_text()


def test_input_grows_with_text_up_to_a_limit(qtbot, make_win):
    w = make_win(ScriptedBackend([Delta("ok")]))
    one = w.input.height()
    w.input.setPlainText("\n".join(["x"] * 3))
    three = w.input.height()
    w.input.setPlainText("\n".join(["x"] * 30))
    assert one < three < w.input.height() <= w.input.MAX_LINES * w.input.fontMetrics().lineSpacing() + 14


def test_collapsible_memory_panel(qtbot, make_win):
    w = make_win(ScriptedBackend([Delta("ok")]))
    w.show()
    qtbot.waitExposed(w)
    assert w.mem_section.is_expanded() and w.mem_list.isVisible()
    w.mem_section.header.click()
    assert not w.mem_section.is_expanded() and not w.mem_list.isVisible() and "▸" in w.mem_section.header.text()


def test_layout_adapts_to_window_size(qtbot, make_win):
    w = make_win(ScriptedBackend([Delta("ok")]))
    w.resize(1300, 800)
    w.show()
    qtbot.waitExposed(w)
    assert w.sidebar.isVisible() and w.side_panel.isVisible()
    w.resize(900, 700)
    qtbot.waitUntil(lambda: not w.side_panel.isVisible())
    assert w.sidebar.isVisible() and w.center.isVisible()
    w.resize(640, 700)
    qtbot.waitUntil(lambda: not w.sidebar.isVisible())
    assert w.center.isVisible()
    w.resize(1300, 800)
    qtbot.waitUntil(lambda: w.sidebar.isVisible() and w.side_panel.isVisible())
    w.toggle_panel_btn.click()  # recolhimento manual é respeitado
    assert not w.side_panel.isVisible()


# ---- M2: renomear, documentos, citações, tarefas ------------------------------------------------
from nemec.ui.widgets import SourceDialog  # noqa: E402
from tests.pdfs import make_pdf  # noqa: E402


def test_rename_conversation_from_sidebar(qtbot, make_win, monkeypatch):
    w = make_win(ScriptedBackend([Delta("oi"), Delta("de novo")]))
    send(qtbot, w, "primeira mensagem")
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Prova de cálculo", True)))
    w.rename_conv_btn.click()
    assert w.title_label.text() == "Prova de cálculo" and w.conv_list.item(0).text() == "Prova de cálculo"
    w.input.setPlainText("segunda")  # novo turno não sobrescreve o título escolhido
    w.send_btn.click()
    qtbot.waitUntil(lambda: not w.busy, timeout=10000)
    assert w.conv_list.item(0).text() == "Prova de cálculo"
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("   ", True)))
    w.rename_conv_btn.click()
    assert not w.banner.isHidden() and w.conv_list.item(0).text() == "Prova de cálculo"
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("x", False)))
    w.rename_conv_btn.click()  # cancelado: nada muda
    assert w.conv_list.item(0).text() == "Prova de cálculo"


def test_documents_panel_import_list_and_delete(qtbot, make_win, tmp_path, monkeypatch):
    w = make_win(ScriptedBackend([Delta("ok")]))
    pdf, md = tmp_path / "calc.pdf", tmp_path / "bio.md"
    pdf.write_bytes(make_pdf(["Derivadas\nA derivada mede a variacao.", "Integrais\nA integral mede a area."]))
    md.write_text("# Bio\n\nA celula e a unidade da vida.\n", encoding="utf-8")
    bad = tmp_path / "ruim.pdf"
    bad.write_bytes(b"nao e pdf")
    w.start_import([pdf, md, bad])
    qtbot.waitUntil(lambda: not w._import_thread and w.doc_list.count() == 3, timeout=10000)
    texts = [w.doc_list.item(i).text() for i in range(3)]
    assert any("calc" in t and "2 págs." in t for t in texts) and any("bio" in t and "Markdown" in t for t in texts)
    assert any("falhou" in t for t in texts) and not w.banner.isHidden() and "ruim.pdf" in w.banner.text()
    assert w.docs_section.header.text().endswith("(3)") and w.doc_import_btn.isEnabled() and w.attach_btn.isEnabled()
    w.start_import([pdf])  # duplicado: aviso, sem nova importação
    qtbot.waitUntil(lambda: not w._import_thread, timeout=10000)
    assert w.doc_list.count() == 3 and "Já importado" in w.banner.text()
    for i in range(w.doc_list.count()):
        if "bio" in w.doc_list.item(i).text():
            w.doc_list.setCurrentRow(i)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    w.doc_del_btn.click()
    assert w.doc_list.count() == 2 and not any("bio" in w.doc_list.item(i).text() for i in range(2))


def test_citations_are_clickable_and_open_source_dialog(qtbot, make_win, tmp_path):
    w = make_win(ScriptedBackend([Delta("A derivada mede a taxa de variacao [1]. Outra coisa [5].")]))
    pdf = tmp_path / "calc.pdf"
    pdf.write_bytes(make_pdf(["Derivadas", "A derivada mede a taxa de variacao instantanea."]))
    w.start_import([pdf])
    qtbot.waitUntil(lambda: not w._import_thread and w.doc_list.count() == 1, timeout=10000)
    send(qtbot, w, "o que e a derivada? taxa de variacao")
    msg = w.view.message_widgets[-1]
    assert "href='cite:1'" in msg.body.text() and "cite:5" not in msg.body.text() and "[5]" not in msg.body.text()
    chips = [b for b in msg.findChildren(type(w.new_btn), "sourceChip")]
    assert len(chips) == 1 and "calc.pdf" in chips[0].toolTip() and "p. 2" in chips[0].toolTip()
    chips[0].click()
    dlg = w._source_dialog
    assert isinstance(dlg, SourceDialog) and "calc.pdf" in dlg.header.text() and "p. 2" in dlg.header.text()
    assert "taxa de variacao" in dlg.body.toPlainText()
    dlg.close()
    msg._on_link("cite:1")  # clique no [1] do texto
    assert w._source_dialog.isVisible()
    w._source_dialog.close()
    shown = w._source_dialog
    for evil in ("http://evil.example", "cite:9", "cite:1; x", "file:///etc/passwd"):
        msg._on_link(evil)
    assert w._source_dialog is shown  # só `cite:N` existente é tratado


def test_deleted_source_shows_tombstone(qtbot, make_win, tmp_path, monkeypatch):
    w = make_win(ScriptedBackend([Delta("A derivada mede a taxa de variacao [1].")]))
    pdf = tmp_path / "calc.pdf"
    pdf.write_bytes(make_pdf(["A derivada mede a taxa de variacao instantanea."]))
    w.start_import([pdf])
    qtbot.waitUntil(lambda: not w._import_thread and w.doc_list.count() == 1, timeout=10000)
    send(qtbot, w, "derivada taxa de variacao")
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    w.doc_list.setCurrentRow(0)
    w.doc_del_btn.click()
    msg = w.view.message_widgets[-1]
    chips = msg.findChildren(type(w.new_btn), "sourceChip")
    assert len(chips) == 1 and "Fonte removida" in chips[0].text()
    chips[0].click()
    assert w._source_dialog.header.text() == "Fonte removida" and w._source_dialog.body is None


def test_task_proposal_card_confirm_edit_and_task_panel(qtbot, make_win, monkeypatch, admin_conn):
    w = make_win(ScriptedBackend([Delta("Deixei pronta."), ToolCall("create_task", {"title": "Estudar derivadas"})]))
    send(qtbot, w, "crie a tarefa estudar derivadas")
    card = w.findChildren(ProposalCard)[0]
    assert card.kind == "create_task" and card.title_edit.text() == "Estudar derivadas"
    assert w.task_list.count() == 0 and "proposta" in w.notice.text().lower()  # nada criado sem confirmar
    card.title_edit.setText("")
    assert not card.confirm_btn.isEnabled()
    card.due_edit.setText("amanhã")
    card.title_edit.setText("Estudar integrais")
    assert not card.confirm_btn.isEnabled()  # prazo inválido
    card.due_edit.setText("2026-12-01 09:30")
    card.confirm_btn.click()
    assert w.task_list.count() == 1 and "Estudar integrais" in w.task_list.item(0).text() and "vence" in w.task_list.item(0).text()
    assert not w.findChildren(ProposalCard) and w.tasks_section.header.text().endswith("(1)")
    w.task_list.setCurrentRow(0)
    w.task_done_btn.click()
    assert w.task_list.item(0).text().startswith("☑") and w.task_list.item(0).font().strikeOut()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    w.task_list.setCurrentRow(0)
    w.task_del_btn.click()
    assert w.task_list.count() == 0


def test_task_proposal_reject_creates_nothing(qtbot, make_win, admin_conn):
    w = make_win(ScriptedBackend([Delta("ok"), ToolCall("create_task", {"title": "X"})]))
    send(qtbot, w, "crie a tarefa x")
    w.findChildren(ProposalCard)[0].reject_btn.click()
    assert not w.findChildren(ProposalCard) and w.task_list.count() == 0
    assert admin_conn.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"] == 0


def test_window_without_m2_services_still_works(qtbot, make_chat, memory_service, admin_conn, tmp_path):
    w = MainWindow(make_chat(ScriptedBackend([Delta("oi")])), memory_service, "modelo-teste",
                   QSettings(str(tmp_path / "p.ini"), QSettings.IniFormat))
    qtbot.addWidget(w)
    assert not w.attach_btn.isEnabled() and w.docs_section.isHidden() and w.tasks_section.isHidden()
