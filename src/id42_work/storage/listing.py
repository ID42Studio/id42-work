"""Keyset pages over stored executions.

Memory applies :func:`page_executions` after the shared sort. SQLite and PostgreSQL
push the same predicate into SQL so a page does not load every body. The creation
time compared in SQL is the JSON text :func:`execution_list_key` already uses, so
the three backends agree. No schema migration is required.
"""

from typing import Literal

from id42_work.execution import Execution
from id42_work.storage.codec import execution_list_key, executions_by_age
from id42_work.storage.protocols import ExecutionListQuery

SqlParam = str | int
Dialect = Literal["sqlite", "postgres"]

_UNFILTERED = "SELECT body FROM executions"


def page_executions(
    executions: tuple[Execution, ...], query: ExecutionListQuery
) -> tuple[Execution, ...]:
    """Return the page ``query`` selects from ``executions``."""
    chosen: list[Execution] = []
    marker = _marker(query)
    for execution in executions_by_age(executions):
        if query.restrict_actor and (
            execution.actor_id != query.actor_id or execution.tenant_id != query.tenant_id
        ):
            continue
        if query.status is not None and execution.status.value != query.status:
            continue
        if marker is not None and execution_list_key(execution) <= marker:
            continue
        chosen.append(execution)
        if query.limit is not None and len(chosen) >= query.limit:
            break
    return tuple(chosen)


def execution_select(
    query: ExecutionListQuery | None, *, dialect: Dialect
) -> tuple[str, list[SqlParam]]:
    """SQL for one page, or for every row when ``query`` is omitted."""
    if query is None:
        return _UNFILTERED, []
    return _page_sql(query, dialect=dialect)


def _marker(query: ExecutionListQuery) -> tuple[str, str] | None:
    if query.after_created_at is None or query.after_execution_id is None:
        return None
    return (query.after_created_at, query.after_execution_id)


def _page_sql(query: ExecutionListQuery, *, dialect: Dialect) -> tuple[str, list[SqlParam]]:
    placeholder = "?" if dialect == "sqlite" else "%s"
    created, actor, tenant = _json_texts(dialect)
    clauses: list[str] = []
    params: list[SqlParam] = []
    if query.restrict_actor:
        clauses.append(f"{actor} = {placeholder}")
        params.append(query.actor_id or "")
        if query.tenant_id is None:
            clauses.append(f"{tenant} IS NULL")
        else:
            clauses.append(f"{tenant} = {placeholder}")
            params.append(query.tenant_id)
    if query.status is not None:
        clauses.append(f"status = {placeholder}")
        params.append(query.status)
    if query.after_created_at is not None and query.after_execution_id is not None:
        later = f"{created} > {placeholder}"
        same_time = f"{created} = {placeholder} AND execution_id > {placeholder}"
        clauses.append(f"({later} OR ({same_time}))")
        params.extend((query.after_created_at, query.after_created_at, query.after_execution_id))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = ""
    if query.limit is not None:
        limit = f" LIMIT {placeholder}"
        params.append(query.limit)
    # Fragments above are fixed literals. Values travel only as parameters.
    statement = f"SELECT body FROM executions{where} ORDER BY {created}, execution_id{limit}"  # noqa: S608
    return statement, params


def _json_texts(dialect: Dialect) -> tuple[str, str, str]:
    if dialect == "sqlite":
        return (
            "json_extract(body, '$.created_at')",
            "json_extract(body, '$.actor_id')",
            "json_extract(body, '$.tenant_id')",
        )
    return (
        "(body::jsonb ->> 'created_at')",
        "(body::jsonb ->> 'actor_id')",
        "(body::jsonb ->> 'tenant_id')",
    )
