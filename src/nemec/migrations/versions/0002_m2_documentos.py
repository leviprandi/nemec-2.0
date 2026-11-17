"""M2: documentos, trechos (chunks) com embedding, citações; suporte a Markdown (aprovado em 2026-11-16)

Aditiva sobre a 0001. Sem tabelas novas além das do MVP_SPEC §3 (`documents`, `document_chunks`,
`message_citations`). Adaptação mínima aprovada para Markdown: `documents.source_type`, `page` anulável
(só para `.md`) e `section`/`char_start` em `document_chunks`. `d` = 1024 (bge-m3).
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UP = """
-- A extensão `vector` é criada por migrations/env.py (exige superusuário), antes do SET ROLE nemec_owner.

CREATE TABLE documents (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title           text NOT NULL,
  file_name       text NOT NULL,
  source_type     text NOT NULL DEFAULT 'pdf',
  sha256          text NOT NULL,
  page_count      integer NOT NULL,
  status          text NOT NULL DEFAULT 'processing',
  embedding_model text NOT NULL,
  error           text,
  imported_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT documents_sha256_key UNIQUE (sha256),
  CONSTRAINT documents_sha256_format CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT documents_page_count_positive CHECK (page_count > 0),
  CONSTRAINT documents_status_check CHECK (status IN ('processing','ready','failed','no_text')),
  CONSTRAINT documents_source_type_check CHECK (source_type IN ('pdf','md')),
  CONSTRAINT documents_title_not_blank CHECK (btrim(title) <> '')
);

CREATE TABLE document_chunks (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id   uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal       integer NOT NULL,
  page          integer,
  section       text,
  char_start    integer,
  content       text NOT NULL,
  search_vector tsvector GENERATED ALWAYS AS (to_tsvector('portuguese', content)) STORED,
  embedding     vector(1024) NOT NULL,
  CONSTRAINT document_chunks_document_ordinal_key UNIQUE (document_id, ordinal),
  CONSTRAINT document_chunks_page_check CHECK (page IS NULL OR page >= 1),
  -- PDF: página obrigatória; Markdown: sem página, mas com posição no texto
  CONSTRAINT document_chunks_location_check CHECK (page IS NOT NULL OR char_start IS NOT NULL),
  CONSTRAINT document_chunks_char_start_check CHECK (char_start IS NULL OR char_start >= 0),
  CONSTRAINT document_chunks_content_length CHECK (char_length(content) BETWEEN 1 AND 4000)
);
CREATE INDEX document_chunks_search_idx ON document_chunks USING gin (search_vector);

CREATE TABLE message_citations (
  message_id  uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  ordinal     integer NOT NULL,
  chunk_id    uuid,
  page        integer,
  quote_start integer,
  quote_end   integer,
  PRIMARY KEY (message_id, ordinal),
  CONSTRAINT message_citations_chunk_fkey FOREIGN KEY (chunk_id) REFERENCES document_chunks(id)
    ON DELETE SET NULL (chunk_id),
  CONSTRAINT message_citations_ordinal_positive CHECK (ordinal > 0),
  CONSTRAINT message_citations_quote_check CHECK
    (quote_start IS NULL OR (chunk_id IS NOT NULL AND quote_end IS NOT NULL AND 0 <= quote_start AND quote_start < quote_end))
);
CREATE INDEX message_citations_chunk_idx ON message_citations (chunk_id);

-- Tombstone (G6): o PostgreSQL só permite anular colunas da própria FK; a página e os offsets são
-- zerados por este gatilho, que dispara também na exclusão em cascata do documento.
CREATE FUNCTION nemec.message_citations_tombstone() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  UPDATE nemec.message_citations SET page = NULL, quote_start = NULL, quote_end = NULL WHERE chunk_id = OLD.id;
  RETURN OLD;
END
$fn$;
CREATE TRIGGER document_chunks_tombstone BEFORE DELETE ON document_chunks
  FOR EACH ROW EXECUTE FUNCTION nemec.message_citations_tombstone();

GRANT SELECT, INSERT, UPDATE, DELETE ON documents, document_chunks, message_citations TO nemec_app;
"""

DOWN = """
DROP TABLE message_citations, document_chunks, documents;
DROP FUNCTION nemec.message_citations_tombstone();
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
