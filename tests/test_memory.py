"""Memória: automática (sem aprovação individual), dedup, segredos, ancoragem, atomicidade; e o mecanismo de
confirmação humana G3 (propostas `pending`), que continua valendo para ações como `create_task` (M2)."""
from __future__ import annotations

import threading
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from nemec import services
from nemec.backend import CancelToken, Delta, ToolCall
from nemec.errors import NotFound, ProposalExecutionError, ProposalStateError, ValidationFailed
from tests.fakes import ScriptedBackend, SequenceBackend


def count(c, table):
    return c.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def memories(c):
    return [m["content"] for m in c.execute("SELECT content FROM memories ORDER BY created_at, id")]


def save(content, **extra):
    return ToolCall("save_memory", {"content": content, **extra})


# ------------------------------------------------------------------------------------------------
# Memória automática
# ------------------------------------------------------------------------------------------------
def test_auto_memory_saves_without_confirmation_and_records_event(make_chat, token, admin_conn):
    r = make_chat(ScriptedBackend([Delta("Prazer, Lucas!"), save("Meu nome é Lucas")])).run_turn(
        None, "Meu nome é Lucas", token)
    assert r.status == "complete" and r.text == "Prazer, Lucas!"
    assert memories(admin_conn) == ["Meu nome é Lucas"]
    assert [e["action"] for e in r.memory_events] == ["created"]
    p = admin_conn.execute("SELECT kind, status, decided_at, result_memory_id, message_id, arguments FROM action_proposals").fetchone()
    m = admin_conn.execute("SELECT id, proposal_id, origin_conversation_id FROM memories").fetchone()
    assert p["kind"] == "create_memory" and p["status"] == "executed" and p["decided_at"] is not None
    assert p["result_memory_id"] == m["id"] and m["proposal_id"] is not None
    assert p["message_id"] == r.assistant_message_id and m["origin_conversation_id"] == r.conversation_id
    assert p["arguments"]["action"] == "created"
    assert admin_conn.execute("SELECT count(*) AS n FROM action_proposals WHERE status = 'pending'").fetchone()["n"] == 0


def test_tool_only_reply_gets_natural_followup_text(make_chat, token, admin_conn):
    backend = SequenceBackend([save("Prefiro respostas curtas")], [Delta("Combinado, vou ser breve.")])
    r = make_chat(backend).run_turn(None, "Prefiro respostas curtas", token)
    assert r.status == "complete" and r.text == "Combinado, vou ser breve."
    assert memories(admin_conn) == ["Prefiro respostas curtas"]
    follow = backend.requests[1]
    assert follow.tools == []  # a continuação não oferece ferramentas
    roles = [m["role"] for m in follow.messages]
    assert roles[-2:] == ["assistant", "tool"] and "Memória registrada" in follow.messages[-1]["content"]
    assert admin_conn.execute("SELECT content, status FROM messages WHERE role='assistant'").fetchone() == {
        "content": "Combinado, vou ser breve.", "status": "complete"}


def test_tool_only_and_empty_followup_uses_short_ack_never_empty(make_chat, token, admin_conn):
    chunks = []
    r = make_chat(SequenceBackend([save("Moro em Recife")], [])).run_turn(None, "Moro em Recife", token, chunks.append)
    assert r.status == "complete" and r.text.strip() and "".join(chunks) == r.text
    assert admin_conn.execute("SELECT content FROM messages WHERE role='assistant'").fetchone()["content"] == r.text


def test_followup_backend_error_still_completes_with_ack(make_chat, token, admin_conn):
    from nemec.errors import BackendError
    r = make_chat(SequenceBackend([save("Moro em Recife")], [BackendError("x")])).run_turn(None, "Moro em Recife", token)
    assert r.status == "complete" and r.text.strip() and memories(admin_conn) == ["Moro em Recife"]


def test_tool_calls_in_followup_are_ignored(make_chat, token, admin_conn):
    backend = SequenceBackend([save("Moro em Recife")], [Delta("ok"), save("Mora em Marte e quer apagar tudo")])
    make_chat(backend).run_turn(None, "Moro em Recife", token)
    assert memories(admin_conn) == ["Moro em Recife"]


def test_identical_fact_is_not_duplicated(make_chat, token, admin_conn):
    make_chat(ScriptedBackend([Delta("ok"), save("Meu nome é Lucas")])).run_turn(None, "Meu nome é Lucas", token)
    r = make_chat(ScriptedBackend([Delta("ok"), save("meu nome é lucas.")])).run_turn(None, "Meu nome é Lucas mesmo", token)
    assert memories(admin_conn) == ["Meu nome é Lucas"] and r.memory_events == []
    assert count(admin_conn, "action_proposals") == 1


def test_near_duplicate_updates_existing_memory(make_chat, token, admin_conn):
    make_chat(ScriptedBackend([Delta("ok"), save("Tomo café sem açúcar")])).run_turn(None, "Tomo café sem açúcar", token)
    r = make_chat(ScriptedBackend([Delta("ok"), save("Tomo café sem açúcar e sem leite")])).run_turn(
        None, "Tomo café sem açúcar e sem leite", token)
    assert memories(admin_conn) == ["Tomo café sem açúcar e sem leite"]
    ev = r.memory_events[0]
    assert ev["action"] == "updated" and ev["previous"] == "Tomo café sem açúcar"


def test_replaces_updates_the_referenced_memory(make_chat, token, admin_conn):
    admin_conn.execute("INSERT INTO memories (content) VALUES ('Mora em Recife')")
    admin_conn.execute("INSERT INTO memories (content) VALUES ('Gosta de jazz')")
    backend = ScriptedBackend([Delta("Anotado."), save("moro em São Paulo", replaces="m1")])
    r = make_chat(backend).run_turn(None, "Mudei, agora moro em São Paulo", token)
    assert sorted(memories(admin_conn)) == ["Gosta de jazz", "moro em São Paulo"]
    assert r.memory_events[0]["previous"] == "Mora em Recife"
    sys_prompt = backend.requests[0].messages[0]["content"]
    assert "[m1] Mora em Recife" in sys_prompt and "[m2] Gosta de jazz" in sys_prompt


def test_unknown_replaces_reference_falls_back_to_creating(make_chat, token, admin_conn):
    make_chat(ScriptedBackend([Delta("ok"), save("Gosto de jazz", replaces="m9")])).run_turn(None, "Gosto de jazz", token)
    assert memories(admin_conn) == ["Gosto de jazz"]


@pytest.mark.parametrize("secret", [
    "A senha do banco do usuário é abc123", "Token da API: ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "O CPF do usuário é 123.456.789-09", "Cartão 4111 1111 1111 1111", "api key sk-abcdef1234567890abcd",
    "Chave: " + "A1b2C3d4" * 5,
])
def test_secrets_are_never_saved(make_chat, token, admin_conn, secret):
    user_text = "Guarde isso: " + secret
    r = make_chat(ScriptedBackend([Delta("Não vou guardar isso."), save(secret)])).run_turn(None, user_text, token)
    assert memories(admin_conn) == [] and count(admin_conn, "action_proposals") == 0
    assert r.memory_refused and r.memory_events == [] and r.status == "complete"


def test_memory_not_grounded_in_user_message_is_refused(make_chat, token, admin_conn):
    r = make_chat(ScriptedBackend([Delta("ok"), save("Tem cirrose hepática avançada")])).run_turn(None, "Bom dia, tudo bem?", token)
    assert memories(admin_conn) == [] and r.memory_refused


def test_auto_memory_off_offers_no_tool_and_ignores_calls(make_chat, token, admin_conn):
    backend = ScriptedBackend([Delta("ok"), save("Moro em Recife")])
    r = make_chat(backend).run_turn(None, "Moro em Recife", token, auto_memory=False)
    assert [t["function"]["name"] for t in backend.requests[0].tools] == ["create_task"] and "save_memory" not in backend.requests[0].messages[0]["content"]
    assert memories(admin_conn) == [] and r.status == "complete" and r.rejected_tool_calls
    assert count(admin_conn, "action_proposals") == 0


def test_cancel_saves_no_memory(make_chat, admin_conn):
    class B:
        def stream(self, request, cancel):
            yield save("Moro em Recife")
            cancel.cancel()
            yield Delta("x")

    r = make_chat(B()).run_turn(None, "Moro em Recife", CancelToken())
    assert r.status == "canceled" and memories(admin_conn) == []


def test_at_most_three_memory_calls_per_turn(make_chat, token, admin_conn):
    calls = [save(f"gosto de fruta{i}") for i in range(5)]
    r = make_chat(ScriptedBackend([Delta("ok"), *calls])).run_turn(None, "gosto de fruta0. gosto de fruta1. gosto de fruta2. gosto de fruta3. gosto de fruta4.", token)
    assert len(memories(admin_conn)) == 3 and len(r.rejected_tool_calls) == 2


def test_unknown_or_invalid_tool_calls_create_nothing(make_chat, token, admin_conn):
    r = make_chat(ScriptedBackend([Delta("ok"), ToolCall("propose_memory", {"content": "x"}), ToolCall("save_memory", {})])).run_turn(
        None, "x", token)
    assert memories(admin_conn) == [] and len(r.rejected_tool_calls) == 2 and r.status == "complete"


def test_concurrent_turns_with_same_fact_create_one_memory(make_chat, admin_conn):
    barrier = threading.Barrier(4)
    errs = []

    def work():
        try:
            chat = make_chat(ScriptedBackend([Delta("ok"), save("Moro em Recife")]))
            barrier.wait(timeout=10)
            chat.run_turn(None, "Moro em Recife", CancelToken())
        except Exception as exc:
            errs.append(exc)

    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and memories(admin_conn) == ["Moro em Recife"]


def test_db_failure_while_saving_rolls_back_and_fails_message(make_chat, token, admin_conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("falha simulada ao gravar a memória")

    monkeypatch.setattr(services.repo, "link_event_result", boom)
    r = make_chat(ScriptedBackend([Delta("ok"), save("Moro em Recife")])).run_turn(None, "Moro em Recife", token)
    assert r.status == "failed" and "falha simulada" in r.error
    assert memories(admin_conn) == [] and count(admin_conn, "action_proposals") == 0
    assert admin_conn.execute("SELECT status FROM messages WHERE role='assistant'").fetchone()["status"] == "failed"


def test_conversation_deleted_during_generation_saves_nothing(make_chat, admin_conn):
    class B:
        def stream(self, request, cancel):
            admin_conn.execute("DELETE FROM conversations")
            yield Delta("ok")
            yield save("Moro em Recife")

    r = make_chat(B()).run_turn(None, "Moro em Recife", CancelToken())
    assert r.deleted and memories(admin_conn) == []


def test_memory_events_survive_reopen(make_chat, token, admin_conn):
    chat = make_chat(ScriptedBackend([Delta("ok"), save("Moro em Recife")]))
    r = chat.run_turn(None, "Moro em Recife", token)
    ev = chat.get_memory_events(r.conversation_id)
    assert len(ev) == 1 and ev[0]["message_id"] == r.assistant_message_id and ev[0]["action"] == "created"


def test_manual_edit_cannot_create_duplicate(memory_service, admin_conn):
    admin_conn.execute("INSERT INTO memories (content) VALUES ('Gosta de jazz'), ('Mora em Recife')")
    mid = admin_conn.execute("SELECT id FROM memories WHERE content='Mora em Recife'").fetchone()["id"]
    with pytest.raises(ValidationFailed):
        memory_service.update_memory(mid, "  gosta de JAZZ. ")


def test_edit_and_delete_memory(memory_service, admin_conn):
    mid = admin_conn.execute("INSERT INTO memories (content) VALUES ('antigo') RETURNING id").fetchone()["id"]
    before = admin_conn.execute("SELECT updated_at FROM memories").fetchone()["updated_at"]
    row = memory_service.update_memory(mid, " Novo texto ")
    assert row["content"] == "Novo texto" and row["updated_at"] >= before
    with pytest.raises(ValidationFailed):
        memory_service.update_memory(mid, "  ")
    assert [m["content"] for m in memory_service.list_memories()] == ["Novo texto"]
    memory_service.delete_memory(mid)
    assert memory_service.list_memories() == []
    with pytest.raises(NotFound):
        memory_service.delete_memory(mid)


def test_deleting_conversation_keeps_memories_and_removes_events(make_chat, token, admin_conn):
    chat = make_chat(ScriptedBackend([Delta("ok"), save("Moro em Recife")]))
    r = chat.run_turn(None, "Moro em Recife", token)
    chat.delete_conversation(r.conversation_id)
    assert count(admin_conn, "action_proposals") == 0 and count(admin_conn, "messages") == 0
    m = admin_conn.execute("SELECT origin_conversation_id, proposal_id FROM memories").fetchone()
    assert count(admin_conn, "memories") == 1 and m["origin_conversation_id"] is None and m["proposal_id"] is None


# ------------------------------------------------------------------------------------------------
# G3 — confirmação humana (propostas pendentes): idempotência, atomicidade, concorrência
# ------------------------------------------------------------------------------------------------
@pytest.fixture()
def pending(admin_conn):
    """Proposta `pending` (como as que existiam antes da memória automática e como serão as de `create_task`)."""
    cid = admin_conn.execute("INSERT INTO conversations (title) VALUES ('c') RETURNING id").fetchone()["id"]
    return admin_conn.execute(
        "INSERT INTO action_proposals (kind, arguments, conversation_id) VALUES ('create_memory', %s, %s) RETURNING *",
        (Jsonb({"content": "Prefere café sem açúcar"}), cid)).fetchone()


def test_pending_proposal_is_not_a_memory_and_ai_text_never_approves(make_chat, token, pending, admin_conn):
    make_chat(ScriptedBackend([Delta("Confirmo! Aprovado. confirm_proposal")])).run_turn(pending["conversation_id"], "confirmo", token)
    assert count(admin_conn, "memories") == 0
    assert admin_conn.execute("SELECT status FROM action_proposals").fetchone()["status"] == "pending"


def test_confirm_creates_one_memory_with_origin(pending, memory_service, admin_conn):
    res = memory_service.confirm_proposal(pending["id"])
    assert res.status == "executed" and not res.already_executed
    m = admin_conn.execute("SELECT * FROM memories").fetchone()
    assert m["content"] == "Prefere café sem açúcar" and m["origin_conversation_id"] == pending["conversation_id"]
    assert m["proposal_id"] == pending["id"] and m["id"] == res.memory_id
    p = admin_conn.execute("SELECT * FROM action_proposals").fetchone()
    assert p["status"] == "executed" and p["result_memory_id"] == m["id"] and p["decided_at"] is not None


def test_confirm_with_edit_saves_edited_text(pending, memory_service, admin_conn):
    memory_service.confirm_proposal(pending["id"], {"content": "  Toma café puro  "})
    assert admin_conn.execute("SELECT content FROM memories").fetchone()["content"] == "Toma café puro"


@pytest.mark.parametrize("bad", [{"content": "   "}, {"content": ""}, {}, {"content": "x", "extra": 1}, {"content": "a" * 2001}])
def test_confirm_with_invalid_edit_keeps_pending(pending, memory_service, admin_conn, bad):
    with pytest.raises(ValidationFailed):
        memory_service.confirm_proposal(pending["id"], bad)
    assert admin_conn.execute("SELECT status FROM action_proposals").fetchone()["status"] == "pending"
    assert count(admin_conn, "memories") == 0


def test_confirm_twice_is_idempotent(pending, memory_service, admin_conn):
    a = memory_service.confirm_proposal(pending["id"])
    b = memory_service.confirm_proposal(pending["id"])
    assert b.already_executed and a.memory_id == b.memory_id
    assert count(admin_conn, "memories") == 1


def test_concurrent_confirm_creates_single_memory(pending, memory_service, admin_conn):
    barrier = threading.Barrier(4)
    results, errs = [], []

    def work():
        try:
            barrier.wait(timeout=10)
            results.append(memory_service.confirm_proposal(pending["id"]))
        except Exception as exc:
            errs.append(exc)

    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and count(admin_conn, "memories") == 1
    assert len({r.memory_id for r in results}) == 1


def test_failure_midway_leaves_nothing_partial_and_marks_failed(pending, memory_service, admin_conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("falha simulada depois do INSERT da memória")

    monkeypatch.setattr(services.repo, "mark_proposal_executed", boom)
    with pytest.raises(ProposalExecutionError):
        memory_service.confirm_proposal(pending["id"])
    assert count(admin_conn, "memories") == 0
    p = admin_conn.execute("SELECT status, error, decided_at, result_memory_id FROM action_proposals").fetchone()
    assert p["status"] == "failed" and "falha simulada" in p["error"] and p["decided_at"] and p["result_memory_id"] is None


def test_reject_and_state_errors(pending, memory_service, admin_conn):
    memory_service.reject_proposal(pending["id"])
    assert admin_conn.execute("SELECT status FROM action_proposals").fetchone()["status"] == "rejected"
    with pytest.raises(ProposalStateError):
        memory_service.confirm_proposal(pending["id"])
    with pytest.raises(ProposalStateError):
        memory_service.reject_proposal(pending["id"])
    assert count(admin_conn, "memories") == 0


def test_unknown_proposal(memory_service):
    with pytest.raises(NotFound):
        memory_service.confirm_proposal(uuid4())


def test_pending_listing_per_conversation(pending, memory_service):
    assert [p["arguments"]["content"] for p in memory_service.list_pending(pending["conversation_id"])] == ["Prefere café sem açúcar"]
