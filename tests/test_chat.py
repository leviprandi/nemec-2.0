"""Chat: streaming, cancelamento, falhas, orfãos e contexto."""
from __future__ import annotations

import threading

import pytest

from nemec.backend import ChatRequest, Delta, ToolCall
from nemec.errors import BackendError, BackendUnavailable, ValidationFailed
from tests.fakes import GatedBackend, ScriptedBackend, deltas


def msgs(admin_conn, cid=None):
    return admin_conn.execute("SELECT seq, role, content, status, model FROM messages ORDER BY conversation_id, seq").fetchall()


def test_streaming_persists_complete_turn(make_chat, token, admin_conn):
    backend = ScriptedBackend(deltas("Olá, ", "mundo"))
    got = []
    r = make_chat(backend).run_turn(None, "  oi  ", token, got.append)
    assert got == ["Olá, ", "mundo"] and r.status == "complete" and r.text == "Olá, mundo"
    rows = msgs(admin_conn)
    assert [(x["seq"], x["role"], x["content"], x["status"]) for x in rows] == [
        (1, "user", "oi", "complete"), (2, "assistant", "Olá, mundo", "complete")]
    assert rows[1]["model"] == "modelo-teste"
    assert admin_conn.execute("SELECT title FROM conversations").fetchone()["title"] == "oi"


def test_no_transaction_open_during_generation(make_chat, token, admin_conn):
    seen = {}

    class Probe(ScriptedBackend):
        def stream(self, request, cancel):
            seen["idle_in_tx"] = admin_conn.execute(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name='nemec' "
                "AND state LIKE 'idle in transaction%%'").fetchone()["n"]
            yield Delta("x")

    make_chat(Probe([])).run_turn(None, "oi", token)
    assert seen["idle_in_tx"] == 0


def test_cancel_keeps_partial_text_and_never_complete(make_chat, token, admin_conn):
    backend = GatedBackend("parcial")
    chat = make_chat(backend)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", chat.run_turn(None, "conte", token)))
    t.start()
    assert backend.started.wait(5)
    token.cancel()
    t.join(5)
    r = out["r"]
    assert r.status == "canceled" and r.text == "parcial"
    row = msgs(admin_conn)[1]
    assert row["status"] == "canceled" and row["content"] == "parcial"


def test_cancel_discards_tool_calls(make_chat, token, admin_conn):
    class B:
        def stream(self, request, cancel):
            yield Delta("a")
            cancel.cancel()
            yield ToolCall("save_memory", {"content": "algo"})

    r = make_chat(B()).run_turn(None, "oi", token)
    assert r.status == "canceled"
    assert admin_conn.execute("SELECT count(*) AS n FROM action_proposals").fetchone()["n"] == 0


@pytest.mark.parametrize("exc", [BackendUnavailable("Ollama fora do ar"), BackendError("quebrou"), RuntimeError("bug")])
def test_backend_errors_mark_failed_without_false_success(make_chat, token, admin_conn, exc):
    r = make_chat(ScriptedBackend([Delta("início"), exc])).run_turn(None, "oi", token)
    assert r.status == "failed" and r.error
    assert msgs(admin_conn)[1]["status"] == "failed"
    assert not any(x["status"] == "complete" and x["role"] == "assistant" for x in msgs(admin_conn))


def test_unavailable_ollama_real_backend_fails_cleanly(db, settings, token, admin_conn):
    from dataclasses import replace
    from nemec.backend import OllamaBackend
    from nemec.services import ChatService

    s = replace(settings, ollama_host="http://127.0.0.1:1")  # porta fechada
    r = ChatService(db, OllamaBackend(s.ollama_host, timeout=5), s).run_turn(None, "oi", token)
    assert r.status == "failed" and "Ollama" in r.error
    assert msgs(admin_conn)[1]["status"] == "failed"


def test_empty_response_is_failed(make_chat, token):
    r = make_chat(ScriptedBackend([])).run_turn(None, "oi", token)
    assert r.status == "failed"


def test_orphan_generating_becomes_failed(make_chat, admin_conn):
    cid = admin_conn.execute("INSERT INTO conversations (title) VALUES ('c') RETURNING id").fetchone()["id"]
    admin_conn.execute("INSERT INTO messages (conversation_id, seq, role, content, status) VALUES (%s,1,'assistant','','generating')", (cid,))
    assert make_chat(ScriptedBackend([])).recover_orphans() == 1
    assert admin_conn.execute("SELECT status FROM messages").fetchone()["status"] == "failed"


def test_prompt_uses_only_complete_messages_and_approved_memories(make_chat, token, admin_conn):
    cid = admin_conn.execute("INSERT INTO conversations (title) VALUES ('c') RETURNING id").fetchone()["id"]
    for seq, (role, content, status) in enumerate([
        ("user", "u1", "complete"), ("assistant", "falhou-texto", "failed"),
        ("user", "u2", "complete"), ("assistant", "cancelado-texto", "canceled"),
        ("assistant", "gerando-texto", "generating")], 1):
        admin_conn.execute("INSERT INTO messages (conversation_id, seq, role, content, status) VALUES (%s,%s,%s,%s,%s)",
                           (cid, seq, role, content, status))
    admin_conn.execute("INSERT INTO memories (content) VALUES ('gosto de café')")
    pid = admin_conn.execute("INSERT INTO action_proposals (kind, arguments, conversation_id) VALUES "
                             "('create_memory', '{\"content\":\"pendente-secreta\"}', %s) RETURNING id", (cid,)).fetchone()
    backend = ScriptedBackend(deltas("ok"))
    make_chat(backend).run_turn(cid, "u3", token)
    req: ChatRequest = backend.requests[0]
    blob = "\n".join(m["content"] for m in req.messages)
    assert "gosto de café" in blob and "u1" in blob and "u2" in blob and "u3" in blob
    for banned in ("falhou-texto", "cancelado-texto", "gerando-texto", "pendente-secreta"):
        assert banned not in blob
    assert [t["function"]["name"] for t in req.tools] == ["save_memory", "create_task"]


def test_validation_of_user_text(make_chat, token):
    chat = make_chat(ScriptedBackend(deltas("x")))
    with pytest.raises(ValidationFailed):
        chat.run_turn(None, "   ", token)
    with pytest.raises(ValidationFailed):
        chat.run_turn(None, "a" * 20_001, token)


def test_conversation_deleted_during_generation(make_chat, token, admin_conn):
    chat = make_chat(ScriptedBackend([]))

    class B:
        def stream(self, request, cancel):
            admin_conn.execute("DELETE FROM conversations")
            yield Delta("texto")

    r = make_chat(B()).run_turn(None, "oi", token)
    assert r.deleted and admin_conn.execute("SELECT count(*) AS n FROM messages").fetchone()["n"] == 0


def test_history_list_reopen_delete(make_chat, token, admin_conn):
    chat = make_chat(ScriptedBackend(deltas("r")))
    r = chat.run_turn(None, "primeira", token)
    chat.run_turn(r.conversation_id, "segunda", token)
    assert [c["id"] for c in chat.list_conversations()] == [r.conversation_id]
    assert [m["content"] for m in chat.get_messages(r.conversation_id)] == ["primeira", "r", "segunda", "r"]
    chat.delete_conversation(r.conversation_id)
    assert chat.list_conversations() == []
