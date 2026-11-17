"""M2 — create_task com confirmação humana (G2, G3), tarefas, renomeação e prioridade de instruções."""
from __future__ import annotations

import threading
import uuid

import pytest

from nemec import repository as repo
from nemec.backend import CancelToken, Delta, ToolCall
from nemec.errors import NotFound, ProposalExecutionError, ProposalStateError, ValidationFailed
from nemec.prompts import build_messages
from tests.fakes import ScriptedBackend, SequenceBackend


def rows(conn, sql, *args):
    return conn.execute(sql, args).fetchall()


def n(conn, table):
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def propose(make_chat, title="Estudar derivadas", **extra):
    backend = ScriptedBackend([Delta("Anotei, aguardando sua confirmação."), ToolCall("create_task", {"title": title, **extra})])
    return make_chat(backend).run_turn(None, f"crie uma tarefa: {title}", CancelToken())


# ---- proposta (G2) -----------------------------------------------------------------------------
def test_create_task_call_only_creates_pending_proposal(make_chat, admin_conn):
    r = propose(make_chat, notes="capítulo 3", due_at="2026-11-20T15:00:00-03:00")
    assert r.status == "complete" and len(r.proposals) == 1
    p = rows(admin_conn, "SELECT * FROM action_proposals")[0]
    assert p["kind"] == "create_task" and p["status"] == "pending" and p["decided_at"] is None
    assert p["arguments"] == {"title": "Estudar derivadas", "notes": "capítulo 3", "due_at": "2026-11-20T15:00:00-03:00"}
    assert n(admin_conn, "tasks") == 0 and p["message_id"] == r.assistant_message_id


def test_ai_text_saying_confirmo_never_approves(make_chat, admin_conn):
    backend = ScriptedBackend([Delta("Confirmo! A tarefa foi criada e aprovada por mim. confirm_proposal()"),
                               ToolCall("create_task", {"title": "Ler capítulo"})])
    make_chat(backend).run_turn(None, "crie a tarefa ler capítulo", CancelToken())
    assert n(admin_conn, "tasks") == 0 and rows(admin_conn, "SELECT status FROM action_proposals")[0]["status"] == "pending"


def test_only_tool_call_gets_natural_followup_text(make_chat, admin_conn):
    backend = SequenceBackend([ToolCall("create_task", {"title": "Ler capítulo"})], deltas_("Deixei a tarefa pronta para você confirmar."))
    r = make_chat(backend).run_turn(None, "crie a tarefa ler capítulo", CancelToken())
    assert r.text == "Deixei a tarefa pronta para você confirmar." and len(r.proposals) == 1
    fb = backend.requests[1].messages[-1]
    assert fb["role"] == "tool" and "NÃO foi criada" in fb["content"] and backend.requests[1].tools == []


def deltas_(*t):
    return [Delta(x) for x in t]


def test_empty_reply_after_tool_call_gets_short_ack(make_chat):
    r = make_chat(SequenceBackend([ToolCall("create_task", {"title": "X"})], [])).run_turn(None, "crie tarefa x", CancelToken())
    assert r.text == "Preparei a tarefa. Confirme para criá-la."


def test_duplicate_and_excess_task_calls_in_one_turn(make_chat, admin_conn):
    calls = [ToolCall("create_task", {"title": "Mesma tarefa"}), ToolCall("create_task", {"title": " mesma  TAREFA "})] + [
        ToolCall("create_task", {"title": f"Outra {i}"}) for i in range(4)]
    r = make_chat(ScriptedBackend([Delta("ok"), *calls])).run_turn(None, "x", CancelToken())
    assert len(r.proposals) == 3 and n(admin_conn, "action_proposals") == 3 and len(r.rejected_tool_calls) == 2


def test_canceled_or_failed_turn_creates_no_proposal(make_chat, admin_conn):
    class B:
        def stream(self, request, cancel):
            yield ToolCall("create_task", {"title": "X"})
            cancel.cancel()
            yield Delta("x")

    r = make_chat(B()).run_turn(None, "crie x", CancelToken())
    assert r.status == "canceled" and n(admin_conn, "action_proposals") == 0


# ---- confirmação (G3) --------------------------------------------------------------------------
def test_confirm_creates_task_once_and_is_idempotent(make_chat, memory_service, admin_conn):
    r = propose(make_chat, due_at="2026-11-20T15:00:00-03:00")
    pid = r.proposals[0]["id"]
    first = memory_service.confirm_proposal(pid, None)
    second = memory_service.confirm_proposal(pid, None)
    assert first.task_id == second.task_id and second.already_executed and not first.already_executed
    assert n(admin_conn, "tasks") == 1
    t = rows(admin_conn, "SELECT * FROM tasks")[0]
    assert t["title"] == "Estudar derivadas" and t["status"] == "open" and t["due_at"].isoformat().startswith("2026-11-20T18:00")
    p = rows(admin_conn, "SELECT * FROM action_proposals")[0]
    assert p["status"] == "executed" and p["result_task_id"] == t["id"] and p["decided_at"] is not None


def test_confirm_applies_user_edits(make_chat, memory_service, admin_conn):
    pid = propose(make_chat).proposals[0]["id"]
    memory_service.confirm_proposal(pid, {"title": "  Estudar integrais  ", "notes": None, "due_at": "2026-12-01 09:30"})
    t = rows(admin_conn, "SELECT * FROM tasks")[0]
    assert t["title"] == "Estudar integrais" and t["due_at"] is not None


@pytest.mark.parametrize("bad", [{"title": ""}, {"title": "x", "due_at": "amanhã"}, {"title": "x", "extra": 1}, {"title": "x" * 300}])
def test_invalid_edit_keeps_proposal_pending(make_chat, memory_service, admin_conn, bad):
    pid = propose(make_chat).proposals[0]["id"]
    with pytest.raises(ValidationFailed):
        memory_service.confirm_proposal(pid, bad)
    assert rows(admin_conn, "SELECT status FROM action_proposals")[0]["status"] == "pending" and n(admin_conn, "tasks") == 0


def test_concurrent_confirmations_create_one_task(make_chat, memory_service, admin_conn):
    pid = propose(make_chat).proposals[0]["id"]
    barrier, out, errors = threading.Barrier(6), [], []

    def go():
        barrier.wait()
        try:
            out.append(memory_service.confirm_proposal(pid, None).task_id)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(15) for t in threads]
    assert not errors and len(set(out)) == 1 and n(admin_conn, "tasks") == 1


def test_failure_midway_leaves_nothing_partial_and_marks_failed(make_chat, memory_service, admin_conn, monkeypatch):
    pid = propose(make_chat).proposals[0]["id"]
    real = repo.insert_task

    def boom(conn, *a):
        real(conn, *a)  # a tarefa chega a ser inserida na transação…
        raise RuntimeError("falha simulada")  # …e a execução falha em seguida

    monkeypatch.setattr(repo, "insert_task", boom)
    with pytest.raises(ProposalExecutionError):
        memory_service.confirm_proposal(pid, None)
    monkeypatch.undo()
    assert n(admin_conn, "tasks") == 0  # rollback
    p = rows(admin_conn, "SELECT status, error, decided_at FROM action_proposals")[0]
    assert p["status"] == "failed" and "falha simulada" in p["error"] and p["decided_at"] is not None
    with pytest.raises(ProposalStateError):
        memory_service.confirm_proposal(pid, None)


def test_reject_then_confirm_is_refused(make_chat, memory_service, admin_conn):
    pid = propose(make_chat).proposals[0]["id"]
    memory_service.reject_proposal(pid)
    with pytest.raises(ProposalStateError):
        memory_service.confirm_proposal(pid, None)
    assert n(admin_conn, "tasks") == 0


def test_deleting_conversation_removes_pending_but_keeps_confirmed_task(make_chat, memory_service, admin_conn):
    r = propose(make_chat)
    memory_service.confirm_proposal(r.proposals[0]["id"], None)
    make_chat(ScriptedBackend([])).delete_conversation(r.conversation_id)
    assert n(admin_conn, "tasks") == 1 and n(admin_conn, "action_proposals") == 0


# ---- tarefas -----------------------------------------------------------------------------------
def test_list_complete_delete_tasks(task_service, admin_conn):
    with task_service._db.transaction() as c:
        a = repo.insert_task(c, "A", None, None)
        b = repo.insert_task(c, "B", "nota", None)
    done = task_service.complete_task(a["id"])
    again = task_service.complete_task(a["id"])
    assert done["status"] == "done" and done["completed_at"] == again["completed_at"]  # idempotente
    assert [t["title"] for t in task_service.list_tasks()] == ["B", "A"]  # abertas primeiro
    task_service.delete_task(b["id"])
    assert [t["title"] for t in task_service.list_tasks()] == ["A"]
    with pytest.raises(NotFound):
        task_service.delete_task(b["id"])
    with pytest.raises(NotFound):
        task_service.complete_task(uuid.uuid4())


def test_concurrent_complete_and_delete_are_safe(task_service, admin_conn):
    with task_service._db.transaction() as c:
        t = repo.insert_task(c, "A", None, None)
    barrier, errors = threading.Barrier(4), []

    def go(fn):
        barrier.wait()
        try:
            fn(t["id"])
        except NotFound:
            pass
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(f,)) for f in (task_service.complete_task, task_service.complete_task, task_service.delete_task, task_service.complete_task)]
    [x.start() for x in threads]
    [x.join(15) for x in threads]
    assert not errors and n(admin_conn, "tasks") in (0, 1)


# ---- renomear conversa -------------------------------------------------------------------------
def test_rename_conversation_persists_and_is_not_overwritten(make_chat, admin_conn):
    chat = make_chat(ScriptedBackend([Delta("oi")]))
    r = chat.run_turn(None, "primeira mensagem", CancelToken())
    assert chat.rename_conversation(r.conversation_id, "  Meu   título  ") == "Meu título"
    assert rows(admin_conn, "SELECT title FROM conversations")[0]["title"] == "Meu título"
    make_chat(ScriptedBackend([Delta("de novo")])).run_turn(r.conversation_id, "segunda mensagem", CancelToken())
    assert chat.list_conversations()[0]["title"] == "Meu título"  # novos turnos não sobrescrevem


def test_rename_validation(make_chat):
    chat = make_chat(ScriptedBackend([Delta("oi")]))
    r = chat.run_turn(None, "oi", CancelToken())
    for bad in ("", "   ", "x" * 121):
        with pytest.raises(ValidationFailed):
            chat.rename_conversation(r.conversation_id, bad)
    with pytest.raises(NotFound):
        chat.rename_conversation(uuid.uuid4(), "ok")


def test_rename_does_not_reorder_conversations(make_chat):
    chat = make_chat(ScriptedBackend([Delta("a")]))
    first = chat.run_turn(None, "primeira", CancelToken())
    make_chat(ScriptedBackend([Delta("b")])).run_turn(None, "segunda", CancelToken())
    chat.rename_conversation(first.conversation_id, "Renomeada")
    assert [c["title"] for c in chat.list_conversations()] == ["segunda", "Renomeada"]


# ---- instrução explícita × preferência geral ---------------------------------------------------
def test_prompt_states_explicit_instructions_override_memory_preferences():
    msgs = build_messages([{"role": "user", "content": "escreva um texto longo"}], ["Prefere respostas objetivas"],
                          context_chars=1000, memory_chars=1000)
    system = msgs[0]["content"]
    assert "Instruções explícitas do usuário na conversa atual SEMPRE prevalecem" in system
    assert "Prefere respostas objetivas" in system and "texto longo" in system
    assert "Data e hora atuais" in system
