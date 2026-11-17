"""Serviços de chat (G4) e de memória/propostas (G2, G3)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from nemec import memory_policy as policy
from nemec import repository as repo
from nemec.backend import CancelToken, ChatBackend, ChatRequest, Delta, ToolCall
from nemec.config import Settings
from nemec.db import Database
from nemec.errors import (
    BackendError,
    BackendUnavailable,
    NemecError,
    NotFound,
    ProposalExecutionError,
    ProposalStateError,
    ValidationFailed,
)
import dataclasses
import re

from nemec.documents import Passage, Retriever, plan_citations
from nemec.prompts import build_messages, fit_passages, visible_memory_count
from nemec.tools import (
    KIND_TO_SPEC,
    MEMORY_MAX_CHARS,
    ToolCallRejected,
    ollama_tool_schemas,
    validate_arguments,
    validate_tool_call,
)

MAX_USER_CHARS = 20_000
TITLE_MAX = 60
MAX_MEMORY_CALLS_PER_TURN = 3
MAX_TASK_CALLS_PER_TURN = 3
TITLE_USER_MAX = 120
GENERIC_LIBRARY_QUESTION = re.compile(
    r"\b(arquivos?|documentos?|materi(?:al|ais)|anota[çc](?:ão|ões|ao|oes)|apostilas?|biblioteca|pdfs?)\b", re.I)
ACK_TEXT = {
    "created": "Anotado.", "updated": "Atualizei isso.", "unchanged": "Certo, já estava anotado.",
    "proposed": "Preparei a tarefa. Confirme para criá-la.",
}


@dataclass
class TurnResult:
    conversation_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    status: str  # complete | canceled | failed
    text: str
    error: str | None = None
    proposals: list[dict[str, Any]] = field(default_factory=list)
    rejected_tool_calls: list[str] = field(default_factory=list)
    memory_events: list[dict[str, Any]] = field(default_factory=list)  # memórias criadas/atualizadas no turno
    memory_refused: list[str] = field(default_factory=list)  # motivos legíveis de memórias não guardadas
    deleted: bool = False  # conversa excluída durante a geração
    warnings: list[str] = field(default_factory=list)  # avisos legíveis (busca em documentos, fonte removida…)
    citations: int = 0  # citações gravadas na resposta


def _as_dict(arguments: Any) -> dict[str, Any]:
    return dict(arguments) if isinstance(arguments, dict) else {}


@dataclass
class Decision:
    """Resultado da política de memória para uma chamada `save_memory`."""

    action: str  # created | updated | unchanged | refused | rejected | proposed | duplicate
    content: str = ""
    target: dict[str, Any] | None = None
    reason: str = ""
    args: dict[str, Any] | None = None  # create_task validado

    @property
    def feedback(self) -> str:  # resultado da ferramenta devolvido ao modelo
        return {
            "created": "Memória registrada.",
            "updated": "Memória atualizada.",
            "unchanged": "Já existia uma memória equivalente; nada a fazer.",
            "refused": f"Não foi guardada: {self.reason}. Não tente guardar isso.",
            "rejected": "Chamada inválida ou não permitida; nada foi gravado.",
            "proposed": "Tarefa PROPOSTA; ainda NÃO foi criada: o usuário precisa confirmar na interface.",
            "duplicate": "Essa tarefa já foi proposta neste turno.",
        }[self.action]


def decide_memory_calls(
    calls: list[ToolCall], user_text: str, memories: list[dict[str, Any]], refs: dict[str, UUID]
) -> list[Decision]:
    """Política pura da memória automática, alinhada 1:1 com `calls`. Não grava nada.

    Só `save_memory` com argumentos válidos; sem segredos; ancorada na mensagem do usuário; sem duplicidade
    (idêntica → `unchanged`; muito parecida ou `replaces` → `updated`)."""
    by_id = {m["id"]: m for m in memories}
    working = [dict(m) for m in memories]  # cópia: reflete o que já foi decidido neste turno
    decisions: list[Decision] = []
    for i, call in enumerate(calls):
        if i >= MAX_MEMORY_CALLS_PER_TURN:
            decisions.append(Decision("rejected", reason="excesso de chamadas de memória no turno"))
            continue
        try:
            valid = validate_tool_call(call.name, call.arguments)
        except ToolCallRejected as exc:
            decisions.append(Decision("rejected", reason=str(exc)))
            continue
        content, ref = valid.arguments["content"], valid.arguments.get("replaces")
        secret = policy.secret_reason(content)
        if secret:
            decisions.append(Decision("refused", content, reason=f"parece conter {secret}"))
            continue
        span = policy.literal_span(content, user_text)  # A01: só o trecho literal da mensagem ATUAL é persistido
        if span is None:
            decisions.append(Decision("refused", content, reason="não é um trecho literal do que você disse nesta mensagem"))
            continue
        content = span
        secret = policy.secret_reason(content)
        if secret:
            decisions.append(Decision("refused", content, reason=f"parece conter {secret}"))
            continue
        target = by_id.get(refs.get(ref)) if ref else None
        identical = False
        if target is not None:
            # A06: o conteúdo novo não pode duplicar (normalizado) NENHUMA outra memória, nem a do turno.
            norm = policy.normalize(content)
            twin = next((w for w in working if w["id"] != target["id"] and policy.normalize(w["content"]) == norm), None)
            if twin is not None:
                decisions.append(Decision("unchanged", content, twin))
                continue
            identical = policy.normalize(target["content"]) == norm
        else:
            target, identical = policy.find_duplicate(content, working)
        if target is None:
            working.append({"id": None, "content": content})
            decisions.append(Decision("created", content))
        elif identical or target.get("id") is None:  # idêntica, ou parecida com uma criada neste turno
            decisions.append(Decision("unchanged", content, target))
        else:
            original = by_id[target["id"]]  # linha como está no banco (`working` é só rascunho do turno)
            for w in working:
                if w["id"] == target["id"]:
                    w["content"] = content
            decisions.append(Decision("updated", content, original))
    return decisions


def decide_calls(
    calls: list[ToolCall], user_text: str, memories: list[dict[str, Any]], refs: dict[str, UUID], *, memory_on: bool,
) -> list[Decision]:
    """Decisões alinhadas 1:1 com `calls`: `save_memory` pela política de memória (só se `memory_on`);
    `create_task` vira no máximo uma proposta pendente por tarefa distinta; o resto é rejeitado."""
    mem_idx = [i for i, c in enumerate(calls) if c.name == "save_memory" and memory_on]
    out: list[Decision | None] = [None] * len(calls)
    for i, d in zip(mem_idx, decide_memory_calls([calls[i] for i in mem_idx], user_text, memories, refs)):
        out[i] = d
    seen: set[str] = set()
    n_tasks = 0
    for i, call in enumerate(calls):
        if out[i] is not None:
            continue
        if call.name == "save_memory":
            out[i] = Decision("rejected", reason=f"ferramenta não disponível: {call.name!r}")
            continue
        try:
            valid = validate_tool_call(call.name, call.arguments)
        except ToolCallRejected as exc:
            out[i] = Decision("rejected", reason=str(exc))
            continue
        key = policy.normalize(valid.arguments["title"])
        if key in seen:
            out[i] = Decision("duplicate", valid.arguments["title"])
        elif n_tasks >= MAX_TASK_CALLS_PER_TURN:
            out[i] = Decision("rejected", reason="excesso de propostas de tarefa no turno")
        else:
            seen.add(key)
            n_tasks += 1
            out[i] = Decision("proposed", valid.arguments["title"], args=valid.arguments)
    return [d for d in out if d is not None]


def _title_from(text: str) -> str:
    one_line = " ".join(text.split())
    return one_line[:TITLE_MAX] or "Nova conversa"


class ChatService:
    def __init__(self, db: Database, backend: ChatBackend, settings: Settings, retriever: Retriever | None = None) -> None:
        self._db, self._backend, self._settings, self._retriever = db, backend, settings, retriever

    # -- inicialização -------------------------------------------------------------------------
    def recover_orphans(self) -> int:
        """`generating` órfão (app encerrado no meio) → `failed`."""
        with self._db.transaction() as conn:
            return repo.fail_orphan_generating(conn)

    # -- conversas -----------------------------------------------------------------------------
    def list_conversations(self) -> list[dict[str, Any]]:
        with self._db.transaction() as conn:
            return repo.list_conversations(conn)

    def get_messages(self, conversation_id: UUID) -> list[dict[str, Any]]:
        with self._db.transaction() as conn:
            return repo.list_messages(conn, conversation_id)

    def delete_conversation(self, conversation_id: UUID) -> None:
        with self._db.transaction() as conn:
            if not repo.delete_conversation(conn, conversation_id):
                raise NotFound("Conversa não encontrada.")

    def rename_conversation(self, conversation_id: UUID, title: str) -> str:
        """Título escolhido pelo usuário. O título inicial é gerado uma única vez (na criação); nada o sobrescreve."""
        title = " ".join((title or "").split())
        if not title:
            raise ValidationFailed("O título não pode ficar vazio.")
        if len(title) > TITLE_USER_MAX:
            raise ValidationFailed(f"Título muito longo (máximo {TITLE_USER_MAX} caracteres).")
        with self._db.transaction() as conn:
            if not repo.rename_conversation(conn, conversation_id, title):
                raise NotFound("Conversa não encontrada.")
        return title

    def set_pinned(self, conversation_id: UUID, pinned: bool) -> None:
        with self._db.transaction() as conn:
            if not repo.set_conversation_pinned(conn, conversation_id, pinned):
                raise NotFound("Conversa não encontrada.")

    def get_citations(self, conversation_id: UUID) -> dict[Any, list[dict[str, Any]]]:
        """Citações por mensagem; `content` None = fonte removida (tombstone)."""
        out: dict[Any, list[dict[str, Any]]] = {}
        with self._db.transaction() as conn:
            for r in repo.list_citations(conn, conversation_id):
                out.setdefault(r["message_id"], []).append(r)
        return out

    def get_memory_events(self, conversation_id: UUID) -> list[dict[str, Any]]:
        with self._db.transaction() as conn:
            return [
                {"message_id": r["message_id"], "memory_id": r["result_memory_id"], **r["arguments"]}
                for r in repo.list_memory_events(conn, conversation_id)
            ]

    # -- turno de chat -------------------------------------------------------------------------
    def run_turn(
        self,
        conversation_id: UUID | None,
        text: str,
        cancel: CancelToken,
        on_delta: Callable[[str], None] | None = None,
        *,
        auto_memory: bool = True,
    ) -> TurnResult:
        """`auto_memory=False`: a ferramenta `save_memory` não é oferecida nem executada."""
        text = text.strip()
        if not text:
            raise ValidationFailed("Mensagem vazia.")
        if len(text) > MAX_USER_CHARS:
            raise ValidationFailed(f"Mensagem muito longa (máximo {MAX_USER_CHARS} caracteres).")
        model = self._settings.chat_model

        # T1 (curta): usuário `complete`, resposta `generating`, contexto.
        with self._db.transaction() as conn:
            if conversation_id is None:
                conversation_id = repo.create_conversation(conn, _title_from(text))["id"]
            user = repo.insert_message(conn, conversation_id, "user", text, "complete")
            assistant = repo.insert_message(conn, conversation_id, "assistant", "", "generating", model)
            history = repo.list_messages(conn, conversation_id, only_complete=True)
            memory_rows = repo.list_memories(conn)
        result = TurnResult(conversation_id, user["id"], assistant["id"], "complete", "")
        passages, catalog = self._retrieve(text, result)
        passages = fit_passages(passages, self._settings.retrieval_chars)  # A05: só o que cabe no prompt é citável
        # G2a: com conteúdo recuperado (não confiável) no prompt, a memória automática nem é oferecida nem executada.
        memory_on = auto_memory and not passages
        contents = [m["content"] for m in memory_rows]
        n_visible = visible_memory_count(contents, self._settings.memory_chars)
        refs = {f"m{i}": memory_rows[i - 1]["id"] for i in range(1, n_visible + 1)}

        messages = build_messages(
            history, contents, context_chars=self._settings.context_chars,
            memory_chars=self._settings.memory_chars, auto_memory=memory_on, task_tool=True,
            passages=passages, retrieval_chars=self._settings.retrieval_chars, catalog=catalog,
        )
        request = ChatRequest(
            model=model,
            messages=messages,
            tools=ollama_tool_schemas(("save_memory", "create_task") if memory_on else ("create_task",)),
            num_ctx=self._settings.num_ctx,
            think=self._settings.think,
        )

        # Geração: nenhuma transação aberta.
        parts: list[str] = []
        calls: list[ToolCall] = []
        status = "complete"
        try:
            self._consume(request, cancel, parts, calls, on_delta)
            if cancel.is_set():
                status = "canceled"
            elif not parts and not calls:
                status, result.error = "failed", "O modelo retornou uma resposta vazia."
            elif calls and not parts:
                # Só chamou a ferramenta, sem texto: devolve o resultado ao modelo para que ele responda
                # naturalmente (nunca deixar a resposta vazia). A gravação real acontece em T2.
                feedback = [d.feedback for d in decide_calls(calls, text, memory_rows, refs, memory_on=memory_on)]
                followup = ChatRequest(
                    model=model,
                    messages=[
                        *messages,
                        {"role": "assistant", "content": "",
                         "tool_calls": [{"function": {"name": c.name, "arguments": _as_dict(c.arguments)}} for c in calls]},
                        *({"role": "tool", "tool_name": c.name, "content": f} for c, f in zip(calls, feedback)),
                    ],
                    tools=[],
                    num_ctx=self._settings.num_ctx,
                    think=self._settings.think,
                )
                try:
                    self._consume(followup, cancel, parts, [], on_delta)
                except (BackendUnavailable, BackendError):
                    pass  # a resposta de cortesia abaixo cobre; a memória não depende dela
                if cancel.is_set():
                    status = "canceled"
        except (BackendUnavailable, BackendError) as exc:
            status, result.error = "failed", str(exc)
        except Exception as exc:  # defensivo: nunca deixar `generating` nem sucesso falso
            status, result.error = "failed", f"Erro inesperado na geração: {exc}"

        # T2 (curta): ver `_finish`. Falha de banco aqui desfaz tudo e a resposta vira `failed` (nunca fica `generating`).
        try:
            return self._finish(conversation_id, assistant["id"], status, parts, calls, text, refs, result, memory_on, passages, on_delta)
        except Exception as exc:
            with self._db.transaction() as conn:
                repo.finalize_message(conn, assistant["id"], "".join(parts), "failed")
            result.status, result.error = "failed", f"Falha ao registrar a resposta: {exc}"
            result.memory_events, result.memory_refused, result.proposals, result.citations = [], [], [], 0
            return result

    def _finish(self, conversation_id, assistant_id, status, parts, calls, text, refs, result, memory_on, passages, on_delta) -> TurnResult:
        """Decide as chamadas de ferramenta (memória automática, propostas de tarefa), finaliza a mensagem e, só se
        completa e a conversa ainda existe, grava tudo (inclusive as citações) na mesma transação."""
        with self._db.transaction() as conn:
            decisions: list[Decision] = []
            if status == "complete" and calls:
                if memory_on:
                    repo.lock_memories(conn)
                fresh = repo.list_memories(conn) if memory_on else []
                decisions = decide_calls(calls, text, fresh, refs, memory_on=memory_on)
            content = "".join(parts).strip()
            if status == "complete" and not content and decisions:
                done = [d.action for d in decisions if d.action in ACK_TEXT]
                content = ACK_TEXT[done[0]] if done else "Certo."
                if on_delta is not None:
                    on_delta(content)
            plans = []
            if status == "complete" and passages and content:
                content, plans = self._plan_citations(conn, content, passages, result)
            result.status, result.text = status, content
            if not repo.finalize_message(conn, assistant_id, content, status):
                result.deleted = True
                return result
            for plan in plans:
                repo.insert_citation(conn, assistant_id, plan.ordinal, plan.chunk_id, plan.page, plan.quote_start, plan.quote_end)
            result.citations = len(plans)
            for d in decisions:
                if d.action == "rejected":
                    result.rejected_tool_calls.append(d.reason)
                elif d.action == "refused":
                    result.memory_refused.append(d.reason)
                elif d.action in ("created", "updated"):
                    result.memory_events.append(self._write_memory(conn, conversation_id, assistant_id, d))
                elif d.action == "proposed":
                    result.proposals.append(repo.insert_proposal(conn, conversation_id, assistant_id, "create_task", d.args))
        return result

    @staticmethod
    def _plan_citations(conn, content: str, passages: list[Passage], result: TurnResult):
        """G7: trava (FOR SHARE) os chunks citados; chunk que sumiu (documento excluído durante a resposta) →
        citação omitida e aviso. Só `[n]` válidos viram citação."""
        _, wanted = plan_citations(content, passages)
        locked = repo.lock_chunks_for_share(conn, [w.chunk_id for w in wanted])
        if any(w.chunk_id not in locked for w in wanted):
            result.warnings.append("Uma fonte citada foi removida durante a resposta; a citação foi omitida.")
        alive = [p for p in passages if p.chunk_id in locked]
        return plan_citations(content, alive)

    def _retrieve(self, text: str, result: TurnResult) -> tuple[list[Passage], list[dict[str, Any]] | None]:
        """Catálogo real da biblioteca (sempre, se há serviço de documentos) + trechos relevantes. Pergunta genérica
        sobre os materiais, ou busca sem trechos, ganha também a abertura de cada documento (visão geral)."""
        if self._retriever is None:
            return [], None
        try:
            catalog = self._retriever.catalog()
        except Exception as exc:  # defensivo: a conversa continua sem documentos
            result.warnings.append(f"Falha ao consultar seus documentos: {exc}")
            return [], None
        if not any(d["status"] == "ready" and d["embedding_model"] == self._settings.embed_model for d in catalog):
            return [], catalog
        passages: list[Passage] = []
        try:
            passages = self._retriever.retrieve(text)
        except (BackendUnavailable, BackendError) as exc:
            result.warnings.append(f"Não consegui consultar seus documentos: {exc}")
        except Exception as exc:
            result.warnings.append(f"Falha ao consultar seus documentos: {exc}")
        try:
            if GENERIC_LIBRARY_QUESTION.search(text):  # só com intenção de consultar a biblioteca (A02)
                seen = {p.chunk_id for p in passages}
                extra = [p for p in self._retriever.overview() if p.chunk_id not in seen]
                passages = [dataclasses.replace(p, n=i) for i, p in enumerate([*passages, *extra], start=1)]
        except Exception as exc:
            result.warnings.append(f"Falha ao consultar seus documentos: {exc}")
        return passages, catalog

    # -- auxiliares do turno -------------------------------------------------------------------
    def _consume(self, request: ChatRequest, cancel: CancelToken, parts: list[str], calls: list[ToolCall], on_delta) -> None:
        for event in self._backend.stream(request, cancel):
            if cancel.is_set():
                break
            if isinstance(event, Delta):
                parts.append(event.text)
                if on_delta is not None:
                    on_delta(event.text)
            elif isinstance(event, ToolCall):
                calls.append(event)

    @staticmethod
    def _write_memory(conn, conversation_id: UUID, message_id: UUID, d: "Decision") -> dict[str, Any]:
        previous = d.target["content"] if d.target else None
        event = repo.insert_auto_memory_event(
            conn, conversation_id, message_id,
            {"content": d.content, "action": d.action, "previous": previous},
        )
        if d.action == "created":
            memory = repo.insert_memory(conn, d.content, conversation_id, event["id"])
        else:
            memory = repo.update_memory(conn, d.target["id"], d.content)
        repo.link_event_result(conn, event["id"], memory["id"])
        return {"action": d.action, "memory_id": memory["id"], "content": d.content, "previous": previous}


@dataclass(frozen=True)
class ConfirmResult:
    proposal_id: UUID
    status: str
    memory_id: UUID | None = None
    task_id: UUID | None = None
    already_executed: bool = False


def _clean_memory(content: str) -> str:
    content = (content or "").strip()
    if not content:
        raise ValidationFailed("A memória não pode ficar vazia.")
    if len(content) > MEMORY_MAX_CHARS:
        raise ValidationFailed(f"Memória muito longa (máximo {MEMORY_MAX_CHARS} caracteres).")
    return content


class MemoryService:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- memórias aprovadas --------------------------------------------------------------------
    def list_memories(self) -> list[dict[str, Any]]:
        with self._db.transaction() as conn:
            return repo.list_memories(conn)

    def update_memory(self, memory_id: UUID, content: str) -> dict[str, Any]:
        content = _clean_memory(content)
        with self._db.transaction() as conn:
            repo.lock_memories(conn)
            if any(
                m["id"] != memory_id and policy.normalize(m["content"]) == policy.normalize(content)
                for m in repo.list_memories(conn)
            ):
                raise ValidationFailed("Já existe uma memória igual a esta.")
            row = repo.update_memory(conn, memory_id, content)
        if row is None:
            raise NotFound("Memória não encontrada.")
        return row

    def delete_memory(self, memory_id: UUID) -> None:
        with self._db.transaction() as conn:
            if not repo.delete_memory(conn, memory_id):
                raise NotFound("Memória não encontrada.")

    # -- propostas -----------------------------------------------------------------------------
    def list_pending(self, conversation_id: UUID) -> list[dict[str, Any]]:
        with self._db.transaction() as conn:
            return repo.list_pending_proposals(conn, conversation_id)

    def reject_proposal(self, proposal_id: UUID) -> None:
        with self._db.transaction() as conn:
            row = repo.lock_proposal(conn, proposal_id)
            if row is None:
                raise NotFound("Proposta não encontrada.")
            if row["status"] != "pending":
                raise ProposalStateError(f"Proposta já está '{row['status']}'.")
            repo.mark_proposal_rejected(conn, proposal_id)

    def confirm_proposal(self, proposal_id: UUID, edited_arguments: dict[str, Any] | None = None) -> ConfirmResult:
        """G3: consumo atômico e idempotente. Só esta chamada (acionada pela UI) cria a memória ou a tarefa."""
        try:
            with self._db.transaction() as conn:
                row = repo.lock_proposal(conn, proposal_id)
                if row is None:
                    raise NotFound("Proposta não encontrada.")
                if row["status"] == "executed":
                    return ConfirmResult(
                        proposal_id, "executed", row["result_memory_id"], row["result_task_id"], already_executed=True
                    )
                if row["status"] != "pending":
                    raise ProposalStateError(f"Proposta já está '{row['status']}'.")
                spec = KIND_TO_SPEC[row["kind"]]
                try:  # entrada do usuário (editada) ou a original: inválida → rollback, continua pendente
                    args = validate_arguments(spec, edited_arguments if edited_arguments is not None else row["arguments"])
                except ToolCallRejected as exc:
                    raise ValidationFailed(str(exc)) from exc
                if row["kind"] == "create_task":
                    task = repo.insert_task(conn, args["title"], args["notes"], _parse_due(args["due_at"]))
                    repo.mark_proposal_executed(conn, proposal_id, args, result_task_id=task["id"])
                    return ConfirmResult(proposal_id, "executed", task_id=task["id"])
                memory = repo.insert_memory(conn, args["content"], row["conversation_id"], proposal_id)
                repo.mark_proposal_executed(conn, proposal_id, args, result_memory_id=memory["id"])
                return ConfirmResult(proposal_id, "executed", memory["id"])
        except NemecError:
            raise
        except Exception as exc:
            # Rollback já ocorreu; registra a falha em transação nova.
            with self._db.transaction() as conn:
                repo.mark_proposal_failed(conn, proposal_id, f"{type(exc).__name__}: {exc}")
            raise ProposalExecutionError(f"Falha ao confirmar a proposta: {exc}") from exc


def _parse_due(value: str | None) -> datetime | None:
    if not value:
        return None
    due = datetime.fromisoformat(value)
    return due if due.tzinfo else due.astimezone()  # sem fuso: fuso local da máquina


class TaskService:
    """Listagem, conclusão e exclusão de tarefas (edições do próprio usuário não passam por proposta)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._db.transaction() as conn:
            return repo.list_tasks(conn)

    def complete_task(self, task_id: UUID) -> dict[str, Any]:
        with self._db.transaction() as conn:
            row = repo.complete_task(conn, task_id)
        if row is None:
            raise NotFound("Tarefa não encontrada.")
        return row

    def reopen_task(self, task_id: UUID) -> dict[str, Any]:
        with self._db.transaction() as conn:
            row = repo.reopen_task(conn, task_id)
        if row is None:
            raise NotFound("Tarefa não encontrada.")
        return row

    def delete_task(self, task_id: UUID) -> None:
        with self._db.transaction() as conn:
            if not repo.delete_task(conn, task_id):
                raise NotFound("Tarefa não encontrada.")
