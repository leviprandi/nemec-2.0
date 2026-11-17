"""Camada de repositório (SQL parametrizado). Funções recebem a conexão/transação do chamador."""
from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

Row = dict[str, Any]


# ---- conversas ---------------------------------------------------------------------------------
def create_conversation(conn: psycopg.Connection, title: str) -> Row:
    return conn.execute("INSERT INTO conversations (title) VALUES (%s) RETURNING *", (title,)).fetchone()


def list_conversations(conn: psycopg.Connection) -> list[Row]:
    return conn.execute("SELECT * FROM conversations ORDER BY is_pinned DESC, updated_at DESC, created_at DESC").fetchall()


def get_conversation(conn: psycopg.Connection, conversation_id: UUID) -> Row | None:
    return conn.execute("SELECT * FROM conversations WHERE id = %s", (conversation_id,)).fetchone()


def rename_conversation(conn: psycopg.Connection, conversation_id: UUID, title: str) -> bool:
    """Só o título; `updated_at` não muda (a ordem da lista não deve pular por causa de uma renomeação)."""
    return conn.execute("UPDATE conversations SET title = %s WHERE id = %s", (title, conversation_id)).rowcount == 1


def set_conversation_pinned(conn: psycopg.Connection, conversation_id: UUID, pinned: bool) -> bool:
    return conn.execute("UPDATE conversations SET is_pinned = %s WHERE id = %s", (pinned, conversation_id)).rowcount == 1


def delete_conversation(conn: psycopg.Connection, conversation_id: UUID) -> bool:
    return conn.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,)).rowcount == 1


# ---- mensagens ---------------------------------------------------------------------------------
def insert_message(
    conn: psycopg.Connection,
    conversation_id: UUID,
    role: str,
    content: str,
    status: str,
    model: str | None = None,
) -> Row:
    """Aloca `seq` sob lock da linha da conversa (SELECT … FOR UPDATE), depois max(seq)+1 e INSERT,
    tudo na mesma transação (G4). O UNIQUE(conversation_id, seq) é a garantia final."""
    locked = conn.execute("SELECT id FROM conversations WHERE id = %s FOR UPDATE", (conversation_id,)).fetchone()
    if locked is None:
        raise LookupError(f"conversa inexistente: {conversation_id}")
    seq = conn.execute(
        "SELECT COALESCE(max(seq), 0) + 1 AS seq FROM messages WHERE conversation_id = %s", (conversation_id,)
    ).fetchone()["seq"]
    row = conn.execute(
        "INSERT INTO messages (conversation_id, seq, role, content, status, model) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
        (conversation_id, seq, role, content, status, model),
    ).fetchone()
    conn.execute("UPDATE conversations SET updated_at = now() WHERE id = %s", (conversation_id,))
    return row


def finalize_message(conn: psycopg.Connection, message_id: UUID, content: str, status: str) -> bool:
    """Só transita a partir de `generating` (False se a conversa foi excluída ou já finalizada)."""
    return (
        conn.execute(
            "UPDATE messages SET content = %s, status = %s WHERE id = %s AND status = 'generating'",
            (content, status, message_id),
        ).rowcount
        == 1
    )


def list_messages(conn: psycopg.Connection, conversation_id: UUID, *, only_complete: bool = False) -> list[Row]:
    sql = "SELECT * FROM messages WHERE conversation_id = %s"
    if only_complete:
        sql += " AND status = 'complete'"
    return conn.execute(sql + " ORDER BY seq", (conversation_id,)).fetchall()


def fail_orphan_generating(conn: psycopg.Connection) -> int:
    return conn.execute("UPDATE messages SET status = 'failed' WHERE status = 'generating'").rowcount


# ---- propostas ---------------------------------------------------------------------------------
def insert_proposal(
    conn: psycopg.Connection, conversation_id: UUID, message_id: UUID | None, kind: str, arguments: dict[str, Any]
) -> Row:
    return conn.execute(
        "INSERT INTO action_proposals (kind, arguments, conversation_id, message_id) "
        "VALUES (%s, %s, %s, %s) RETURNING *",
        (kind, Jsonb(arguments), conversation_id, message_id),
    ).fetchone()


def lock_proposal(conn: psycopg.Connection, proposal_id: UUID) -> Row | None:
    return conn.execute("SELECT * FROM action_proposals WHERE id = %s FOR UPDATE", (proposal_id,)).fetchone()


def list_pending_proposals(conn: psycopg.Connection, conversation_id: UUID) -> list[Row]:
    return conn.execute(
        "SELECT * FROM action_proposals WHERE conversation_id = %s AND status = 'pending' ORDER BY created_at",
        (conversation_id,),
    ).fetchall()


def get_proposal(conn: psycopg.Connection, proposal_id: UUID) -> Row | None:
    return conn.execute("SELECT * FROM action_proposals WHERE id = %s", (proposal_id,)).fetchone()


def mark_proposal_executed(
    conn: psycopg.Connection,
    proposal_id: UUID,
    arguments: dict[str, Any],
    *,
    result_memory_id: UUID | None = None,
    result_task_id: UUID | None = None,
) -> None:
    conn.execute(
        "UPDATE action_proposals SET status = 'executed', arguments = %s, result_memory_id = %s, "
        "result_task_id = %s, decided_at = now() WHERE id = %s AND status = 'pending'",
        (Jsonb(arguments), result_memory_id, result_task_id, proposal_id),
    )


def mark_proposal_rejected(conn: psycopg.Connection, proposal_id: UUID) -> bool:
    return (
        conn.execute(
            "UPDATE action_proposals SET status = 'rejected', decided_at = now() "
            "WHERE id = %s AND status = 'pending'",
            (proposal_id,),
        ).rowcount
        == 1
    )


def mark_proposal_failed(conn: psycopg.Connection, proposal_id: UUID, error: str) -> bool:
    return (
        conn.execute(
            "UPDATE action_proposals SET status = 'failed', error = %s, decided_at = now() "
            "WHERE id = %s AND status = 'pending'",
            (error[:1000], proposal_id),
        ).rowcount
        == 1
    )


# ---- memórias ----------------------------------------------------------------------------------
def insert_memory(
    conn: psycopg.Connection, content: str, origin_conversation_id: UUID | None, proposal_id: UUID | None
) -> Row:
    return conn.execute(
        "INSERT INTO memories (content, origin_conversation_id, proposal_id) VALUES (%s, %s, %s) RETURNING *",
        (content, origin_conversation_id, proposal_id),
    ).fetchone()


def list_memories(conn: psycopg.Connection) -> list[Row]:
    return conn.execute("SELECT * FROM memories ORDER BY created_at, id").fetchall()


def lock_memories(conn: psycopg.Connection) -> None:
    """Serializa gravações automáticas de memória (evita duplicidade entre turnos concorrentes)."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('nemec.memories'))")


def insert_auto_memory_event(
    conn: psycopg.Connection, conversation_id: UUID, message_id: UUID | None, arguments: dict[str, Any]
) -> Row:
    """Registro de auditoria de uma memória automática (já `executed`; nunca passa por `pending`)."""
    return conn.execute(
        "INSERT INTO action_proposals (kind, arguments, status, conversation_id, message_id, decided_at) "
        "VALUES ('create_memory', %s, 'executed', %s, %s, now()) RETURNING *",
        (Jsonb(arguments), conversation_id, message_id),
    ).fetchone()


def link_event_result(conn: psycopg.Connection, event_id: UUID, memory_id: UUID) -> None:
    conn.execute("UPDATE action_proposals SET result_memory_id = %s WHERE id = %s", (memory_id, event_id))


def list_memory_events(conn: psycopg.Connection, conversation_id: UUID) -> list[Row]:
    """Memórias criadas/atualizadas automaticamente nesta conversa (propostas executadas com `action`)."""
    return conn.execute(
        "SELECT id, message_id, arguments, result_memory_id FROM action_proposals "
        "WHERE conversation_id = %s AND kind = 'create_memory' AND status = 'executed' "
        "AND arguments ? 'action' ORDER BY created_at",
        (conversation_id,),
    ).fetchall()


def get_memory(conn: psycopg.Connection, memory_id: UUID) -> Row | None:
    return conn.execute("SELECT * FROM memories WHERE id = %s", (memory_id,)).fetchone()


def update_memory(conn: psycopg.Connection, memory_id: UUID, content: str) -> Row | None:
    return conn.execute(
        "UPDATE memories SET content = %s, updated_at = now() WHERE id = %s RETURNING *", (content, memory_id)
    ).fetchone()


def delete_memory(conn: psycopg.Connection, memory_id: UUID) -> bool:
    return conn.execute("DELETE FROM memories WHERE id = %s", (memory_id,)).rowcount == 1


# ---- tarefas -----------------------------------------------------------------------------------
def insert_task(conn: psycopg.Connection, title: str, notes: str | None, due_at: Any) -> Row:
    return conn.execute(
        "INSERT INTO tasks (title, notes, due_at) VALUES (%s, %s, %s) RETURNING *", (title, notes, due_at)
    ).fetchone()


def list_tasks(conn: psycopg.Connection) -> list[Row]:
    return conn.execute(
        "SELECT * FROM tasks ORDER BY (status = 'done'), due_at NULLS LAST, created_at, id"
    ).fetchall()


def complete_task(conn: psycopg.Connection, task_id: UUID) -> Row | None:
    """Idempotente: concluir uma tarefa já concluída mantém o `completed_at` original."""
    return conn.execute(
        "UPDATE tasks SET status = 'done', completed_at = COALESCE(completed_at, now()) WHERE id = %s RETURNING *",
        (task_id,),
    ).fetchone()


def reopen_task(conn: psycopg.Connection, task_id: UUID) -> Row | None:
    return conn.execute(
        "UPDATE tasks SET status = 'open', completed_at = NULL WHERE id = %s RETURNING *", (task_id,)
    ).fetchone()


def delete_task(conn: psycopg.Connection, task_id: UUID) -> bool:
    return conn.execute("DELETE FROM tasks WHERE id = %s", (task_id,)).rowcount == 1


# ---- documentos --------------------------------------------------------------------------------
def get_document_by_sha(conn: psycopg.Connection, sha256: str) -> Row | None:
    return conn.execute("SELECT * FROM documents WHERE sha256 = %s", (sha256,)).fetchone()


def insert_document(
    conn: psycopg.Connection, title: str, file_name: str, source_type: str, sha256: str, embedding_model: str
) -> Row:
    return conn.execute(
        "INSERT INTO documents (title, file_name, source_type, sha256, page_count, status, embedding_model) "
        "VALUES (%s, %s, %s, %s, 1, 'processing', %s) RETURNING *",
        (title, file_name, source_type, sha256, embedding_model),
    ).fetchone()


def lock_processing_document(conn: psycopg.Connection, document_id: UUID) -> Row | None:
    """G5-T2: só publica quem ainda encontra o documento em `processing` (excluído → None)."""
    return conn.execute(
        "SELECT * FROM documents WHERE id = %s AND status = 'processing' FOR UPDATE", (document_id,)
    ).fetchone()


def insert_chunk(conn: psycopg.Connection, document_id: UUID, ordinal: int, chunk: Any, vector: list[float]) -> None:
    conn.execute(
        "INSERT INTO document_chunks (document_id, ordinal, page, section, char_start, content, embedding) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::vector)",
        (document_id, ordinal, chunk.page, chunk.section, chunk.char_start, chunk.content, vector_literal(vector)),
    )


def finish_document(conn: psycopg.Connection, document_id: UUID, status: str, page_count: int, error: str | None) -> None:
    conn.execute(
        "UPDATE documents SET status = %s, page_count = %s, error = %s WHERE id = %s",
        (status, max(page_count, 1), error, document_id),
    )


def fail_document(conn: psycopg.Connection, document_id: UUID, error: str) -> None:
    conn.execute(
        "UPDATE documents SET status = 'failed', error = %s WHERE id = %s AND status = 'processing'",
        (error[:1000], document_id),
    )


def list_documents(conn: psycopg.Connection) -> list[Row]:
    return conn.execute(
        "SELECT d.*, (SELECT count(*) FROM document_chunks c WHERE c.document_id = d.id) AS chunk_count "
        "FROM documents d ORDER BY imported_at DESC, id"
    ).fetchall()


def delete_document(conn: psycopg.Connection, document_id: UUID) -> Row | None:
    """G6: chunks em cascata; citações viram tombstone. Devolve título e arquivo do documento removido."""
    return conn.execute(
        "DELETE FROM documents WHERE id = %s RETURNING id, title, file_name", (document_id,)
    ).fetchone()


def fail_orphan_documents(conn: psycopg.Connection) -> int:
    return conn.execute(
        "UPDATE documents SET status = 'failed', error = 'Importação interrompida; importe novamente.' "
        "WHERE status = 'processing'"
    ).rowcount


def has_ready_documents(conn: psycopg.Connection, embedding_model: str) -> bool:
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM documents WHERE status = 'ready' AND embedding_model = %s) AS x",
        (embedding_model,),
    ).fetchone()["x"]


def overview_chunks(conn: psycopg.Connection, embedding_model: str, limit: int) -> list[Row]:
    """Um trecho de abertura por documento `ready` (o mais substancial entre os primeiros), do mais recente ao mais
    antigo: dá uma visão geral da biblioteca para perguntas genéricas ("o que falam esses arquivos?")."""
    return conn.execute(
        "SELECT * FROM ("
        " SELECT DISTINCT ON (d.id) c.id AS chunk_id, c.document_id, c.ordinal, c.page, c.section, c.char_start, c.content,"
        "        d.title, d.file_name, d.source_type, d.imported_at"
        "   FROM document_chunks c JOIN documents d ON d.id = c.document_id"
        "  WHERE d.status = 'ready' AND d.embedding_model = %s AND c.ordinal <= 5"
        "  ORDER BY d.id, (char_length(c.content) < 200), c.ordinal) t"
        " ORDER BY imported_at DESC, document_id LIMIT %s",
        (embedding_model, limit),
    ).fetchall()


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"


# Recuperação híbrida (G7): UMA consulta; os filtros valem antes do LIMIT de cada ramo; FTS `portuguese` (termos
# da pergunta combinados por OU) + distância de cosseno exata + RRF (k = 60).
RETRIEVE_SQL = """
WITH q AS (
  SELECT replace(plainto_tsquery('portuguese', %(text)s)::text, '&', '|')::tsquery AS tsq,
         %(vec)s::vector AS v
), eligible AS (
  SELECT c.id, c.search_vector, c.embedding
    FROM document_chunks c JOIN documents d ON d.id = c.document_id
   WHERE d.status = 'ready' AND d.embedding_model = %(model)s
), fts AS (
  SELECT e.id, row_number() OVER (ORDER BY ts_rank_cd(e.search_vector, q.tsq) DESC, e.id) AS r
    FROM eligible e, q WHERE q.tsq::text <> '' AND e.search_vector @@ q.tsq
   ORDER BY ts_rank_cd(e.search_vector, q.tsq) DESC, e.id LIMIT %(pool)s
), vec AS (
  SELECT e.id, e.embedding <=> q.v AS dist, row_number() OVER (ORDER BY e.embedding <=> q.v, e.id) AS r
    FROM eligible e, q ORDER BY e.embedding <=> q.v, e.id LIMIT %(pool)s
)
SELECT c.id AS chunk_id, c.document_id, c.ordinal, c.page, c.section, c.char_start, c.content,
       d.title, d.file_name, d.source_type,
       COALESCE(1.0 / (60 + f.r), 0) + COALESCE(1.0 / (60 + v.r), 0) AS score,
       f.r AS fts_rank, v.r AS vec_rank, c.embedding <=> q.v AS dist
  FROM fts f FULL JOIN vec v ON v.id = f.id
  JOIN document_chunks c ON c.id = COALESCE(f.id, v.id)
  JOIN documents d ON d.id = c.document_id
  CROSS JOIN q
 WHERE c.embedding <=> q.v <= %(max_dist)s OR (f.id IS NOT NULL AND c.embedding <=> q.v <= %(fts_dist)s)
 ORDER BY score DESC, c.id
 LIMIT %(k)s
"""


def retrieve(
    conn: psycopg.Connection, text: str, vector: list[float], model: str, *, k: int, pool: int, max_dist: float, fts_dist: float
) -> list[Row]:
    """Relevância mínima: passa quem está perto no espaço vetorial (`max_dist`) ou casou no FTS e não está longe
    demais (`fts_dist`); do contrário nenhuma passagem é devolvida (a pergunta não é sobre os documentos)."""
    return conn.execute(
        RETRIEVE_SQL,
        {"text": text, "vec": vector_literal(vector), "model": model, "k": k, "pool": pool,
         "max_dist": max_dist, "fts_dist": fts_dist},
    ).fetchall()


# ---- citações ----------------------------------------------------------------------------------
def lock_chunks_for_share(conn: psycopg.Connection, chunk_ids: list[UUID]) -> dict[UUID, Row]:
    """G7: trava os chunks citados (um DELETE concorrente espera; um já commitado os faz sumir daqui)."""
    rows = conn.execute(
        "SELECT id, page FROM document_chunks WHERE id = ANY(%s) FOR SHARE", (list(chunk_ids),)
    ).fetchall()
    return {r["id"]: r for r in rows}


def insert_citation(
    conn: psycopg.Connection, message_id: UUID, ordinal: int, chunk_id: UUID, page: int | None,
    quote_start: int | None, quote_end: int | None,
) -> None:
    conn.execute(
        "INSERT INTO message_citations (message_id, ordinal, chunk_id, page, quote_start, quote_end) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (message_id, ordinal, chunk_id, page, quote_start, quote_end),
    )


def list_citations(conn: psycopg.Connection, conversation_id: UUID) -> list[Row]:
    """Citações das mensagens da conversa; fonte removida → campos do chunk/documento NULL (tombstone)."""
    return conn.execute(
        "SELECT mc.message_id, mc.ordinal, mc.chunk_id, mc.page, mc.quote_start, mc.quote_end, "
        "       c.content, c.section, c.char_start, d.title, d.file_name, d.source_type "
        "  FROM message_citations mc JOIN messages m ON m.id = mc.message_id "
        "  LEFT JOIN document_chunks c ON c.id = mc.chunk_id LEFT JOIN documents d ON d.id = c.document_id "
        " WHERE m.conversation_id = %s ORDER BY mc.message_id, mc.ordinal",
        (conversation_id,),
    ).fetchall()
