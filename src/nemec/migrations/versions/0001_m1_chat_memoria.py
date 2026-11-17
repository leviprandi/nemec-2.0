"""M1: conversas, mensagens, memórias, propostas (e `tasks`, só esquema para a FK de propostas)

Tabelas do MVP_SPEC §3 necessárias a M1. `tasks` existe apenas para que `action_proposals`
tenha seu contrato final; nenhum código de M1 a utiliza (ferramenta create_task é M2).
`documents`, `document_chunks` e `message_citations` chegam em M2 (dependem do modelo de embedding).
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UP = """
CREATE TABLE conversations (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title       text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT conversations_title_not_blank CHECK (btrim(title) <> '')
);

CREATE TABLE messages (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  seq             bigint NOT NULL,
  role            text NOT NULL,
  content         text NOT NULL,
  status          text NOT NULL,
  model           text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT messages_conversation_seq_key UNIQUE (conversation_id, seq),
  CONSTRAINT messages_seq_positive CHECK (seq > 0),
  CONSTRAINT messages_role_check CHECK (role IN ('user','assistant')),
  CONSTRAINT messages_status_check CHECK (status IN ('generating','complete','canceled','failed'))
);

CREATE TABLE tasks (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title        text NOT NULL,
  notes        text,
  due_at       timestamptz,
  status       text NOT NULL DEFAULT 'open',
  completed_at timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT tasks_title_not_blank CHECK (btrim(title) <> ''),
  CONSTRAINT tasks_status_check CHECK (status IN ('open','done')),
  CONSTRAINT tasks_done_completed_at CHECK ((status = 'done') = (completed_at IS NOT NULL))
);

CREATE TABLE memories (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  content                text NOT NULL,
  origin_conversation_id uuid REFERENCES conversations(id) ON DELETE SET NULL,
  proposal_id            uuid,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT memories_content_not_blank CHECK (btrim(content) <> '')
);

CREATE TABLE action_proposals (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind             text NOT NULL,
  arguments        jsonb NOT NULL,
  status           text NOT NULL DEFAULT 'pending',
  conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  message_id       uuid REFERENCES messages(id) ON DELETE CASCADE,
  result_task_id   uuid REFERENCES tasks(id) ON DELETE SET NULL,
  result_memory_id uuid REFERENCES memories(id) ON DELETE SET NULL,
  error            text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  decided_at       timestamptz,
  CONSTRAINT action_proposals_kind_check CHECK (kind IN ('create_task','create_memory')),
  CONSTRAINT action_proposals_status_check CHECK (status IN ('pending','executed','rejected','failed')),
  CONSTRAINT action_proposals_task_result CHECK
    (result_task_id IS NULL OR (kind = 'create_task' AND status = 'executed')),
  CONSTRAINT action_proposals_memory_result CHECK
    (result_memory_id IS NULL OR (kind = 'create_memory' AND status = 'executed')),
  CONSTRAINT action_proposals_decided_at CHECK ((status = 'pending') = (decided_at IS NULL))
);

ALTER TABLE memories
  ADD CONSTRAINT memories_proposal_id_fkey
  FOREIGN KEY (proposal_id) REFERENCES action_proposals(id) ON DELETE SET NULL;

CREATE INDEX conversations_updated_at_idx ON conversations (updated_at DESC);
CREATE INDEX memories_created_at_idx ON memories (created_at);
CREATE INDEX memories_origin_conversation_idx ON memories (origin_conversation_id);
CREATE INDEX action_proposals_conversation_idx ON action_proposals (conversation_id);
CREATE INDEX action_proposals_pending_idx ON action_proposals (conversation_id) WHERE status = 'pending';

GRANT SELECT, INSERT, UPDATE, DELETE
  ON conversations, messages, tasks, memories, action_proposals TO nemec_app;
"""

DOWN = """
ALTER TABLE memories DROP CONSTRAINT memories_proposal_id_fkey;
DROP TABLE action_proposals, memories, tasks, messages, conversations;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
