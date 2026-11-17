"""Ambiente do Alembic: conecta com a URL administrativa e executa tudo como `nemec_owner`."""
from alembic import context
from sqlalchemy import create_engine, text

config = context.config
url = config.attributes["admin_url"]  # postgresql+psycopg://...
target_schema = "nemec"

engine = create_engine(url)
with engine.connect() as connection:
    # `vector` (pgvector) exige superusuário e não é "trusted": é instalada aqui, antes do SET ROLE, no schema do app.
    connection.execute(text(f"CREATE EXTENSION IF NOT EXISTS vector SCHEMA {target_schema}"))
    connection.execute(text("SET ROLE nemec_owner"))
    connection.execute(text(f"SET search_path TO {target_schema}"))
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=None,
        version_table_schema=target_schema,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
