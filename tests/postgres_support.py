"""Throwaway PostgreSQL databases for tests that set ``ID42_POSTGRES_DSN``."""

import os
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

ENV = "ID42_POSTGRES_DSN"


def configured_dsn() -> str | None:
    """The admin DSN, or ``None`` when PostgreSQL tests should skip."""
    value = os.environ.get(ENV, "").strip()
    return value or None


def create_database(admin_dsn: str) -> tuple[str, str]:
    """Create an empty database. Return its DSN and name."""
    name = f"id42_{uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return make_conninfo(admin_dsn, dbname=name), name


def drop_database(admin_dsn: str, name: str) -> None:
    """Drop ``name``, closing leftover sessions first."""
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
