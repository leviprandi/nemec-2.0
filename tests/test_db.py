"""Migrações, constraints, papéis e concorrência de `seq`."""
from __future__ import annotations

import threading
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors

from nemec import repository as repo
from nemec.db import Database


def one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()


def test_migrations_from_scratch_create_expected_tables(admin_conn):
    names = {r["table_name"] for r in admin_conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='nemec'").fetchall()}
    assert {"conversations", "messages", "memories", "action_proposals", "tasks", "documents", "document_chunks", "message_citations", "alembic_version"} <= names
    assert one(admin_conn, "SELECT version_num FROM alembic_version")["version_num"] == "0003"
    owners = {r["tableowner"] for r in admin_conn.execute("SELECT tableowner FROM pg_tables WHERE schemaname='nemec'")}
    assert owners == {"nemec_owner"}


def new_conv(c, title="t"):
    return one(c, "INSERT INTO conversations (title) VALUES (%s) RETURNING id", title)["id"]


def test_constraints_valid_cases(admin_conn):
    c = admin_conn
    cid = new_conv(c)
    for i, (role, status) in enumerate([("user", "complete"), ("assistant", "generating"),
                                        ("assistant", "canceled"), ("assistant", "failed")], 1):
        c.execute("INSERT INTO messages (conversation_id, seq, role, content, status) VALUES (%s,%s,%s,'x',%s)",
                  (cid, i, role, status))
    c.execute("INSERT INTO memories (content) VALUES ('ok')")
    c.execute("INSERT INTO action_proposals (kind, arguments, conversation_id) VALUES ('create_memory','{}',%s)", (cid,))
    c.execute("INSERT INTO tasks (title) VALUES ('t')")


@pytest.mark.parametrize("sql", [
    "INSERT INTO conversations (title) VALUES ('   ')",
    "INSERT INTO messages (conversation_id, seq, role, content, status) VALUES ({c}, 0, 'user', 'x', 'complete')",
    "INSERT INTO messages (conversation_id, seq, role, content, status) VALUES ({c}, 1, 'system', 'x', 'complete')",
    "INSERT INTO messages (conversation_id, seq, role, content, status) VALUES ({c}, 1, 'user', 'x', 'pending')",
    "INSERT INTO memories (content) VALUES ('  ')",
    "INSERT INTO action_proposals (kind, arguments, conversation_id) VALUES ('shell','{{}}',{c})",
    "INSERT INTO action_proposals (kind, arguments, conversation_id, status) VALUES ('create_memory','{{}}',{c},'maybe')",
    "INSERT INTO action_proposals (kind, arguments, conversation_id, status) VALUES ('create_memory','{{}}',{c},'executed')",  # sem decided_at
    "INSERT INTO action_proposals (kind, arguments, conversation_id, decided_at) VALUES ('create_memory','{{}}',{c}, now())",  # pending com decided_at
    "INSERT INTO tasks (title, status) VALUES ('t','done')",  # done sem completed_at
    "INSERT INTO tasks (title) VALUES ('')",
])
def test_constraints_invalid_cases(admin_conn, sql):
    cid = new_conv(admin_conn)
    stmt = sql.format(c=f"'{cid}'")
    with pytest.raises((errors.CheckViolation, errors.IntegrityError)):
        admin_conn.execute(stmt)


def test_duplicate_seq_rejected_and_cascade(admin_conn):
    cid = new_conv(admin_conn)
    admin_conn.execute("INSERT INTO messages (conversation_id, seq, role, content, status) VALUES (%s,1,'user','x','complete')", (cid,))
    with pytest.raises(errors.UniqueViolation):
        admin_conn.execute("INSERT INTO messages (conversation_id, seq, role, content, status) VALUES (%s,1,'user','y','complete')", (cid,))
    admin_conn.execute("DELETE FROM conversations WHERE id=%s", (cid,))
    assert one(admin_conn, "SELECT count(*) AS n FROM messages")["n"] == 0


def test_app_role_attributes(admin_conn):
    r = one(admin_conn, "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin "
                        "FROM pg_roles WHERE rolname='nemec_app'")
    assert r == {"rolsuper": False, "rolcreatedb": False, "rolcreaterole": False,
                 "rolreplication": False, "rolbypassrls": False, "rolcanlogin": True}
    o = one(admin_conn, "SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname='nemec_owner'")
    assert o == {"rolcanlogin": False, "rolsuper": False}
    member = one(admin_conn, "SELECT count(*) AS n FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member "
                             "WHERE r.rolname='nemec_app'")["n"]
    assert member == 0


@pytest.mark.parametrize("ddl", [
    "CREATE TABLE nemec.intruso (id int)",
    "CREATE TABLE public.intruso (id int)",
    "ALTER TABLE nemec.memories ADD COLUMN x int",
    "DROP TABLE nemec.memories",
    "TRUNCATE nemec.memories",
    "DROP SCHEMA nemec CASCADE",
    "CREATE ROLE intruso",
    "SET ROLE nemec_owner",
    "UPDATE nemec.alembic_version SET version_num='x'",
])
def test_app_role_cannot_ddl(test_app_url, admin_conn, ddl):
    with psycopg.connect(test_app_url, autocommit=True) as c:
        with pytest.raises((errors.InsufficientPrivilege, errors.UndefinedTable)):
            c.execute(ddl)


def test_app_role_can_dml(test_app_url, admin_conn):
    with psycopg.connect(test_app_url, autocommit=True, options="-c search_path=nemec") as c:
        c.execute("INSERT INTO memories (content) VALUES ('x')")
        c.execute("UPDATE memories SET content='y'")
        c.execute("DELETE FROM memories")


def test_concurrent_seq_allocation_is_distinct(db, admin_conn):
    with db.transaction() as c:
        cid = repo.create_conversation(c, "c")["id"]
    n = 8
    barrier = threading.Barrier(n)
    seqs, errs = [], []

    def work(i):
        try:
            with db.transaction() as c:
                barrier.wait(timeout=10)  # todos entram na alocação ao mesmo tempo
                seqs.append(repo.insert_message(c, cid, "user", f"m{i}", "complete")["seq"])
        except Exception as exc:
            errs.append(exc)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    assert sorted(seqs) == list(range(1, n + 1))


def test_insert_message_on_missing_conversation(db):
    with pytest.raises(LookupError):
        with db.transaction() as c:
            repo.insert_message(c, uuid4(), "user", "x", "complete")
