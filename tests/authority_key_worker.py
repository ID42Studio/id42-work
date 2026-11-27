"""Child process for the authority-key restart test.

Prints ``verified`` and nothing else. Arguments are paths and grant names, never
key material or an actor credential.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import UUID

from id42_work import ActorAuthority, ApprovalStatus, SqliteStorage, TrustedActor


def verify(
    *,
    key_path: str,
    reviewer_path: str,
    actor_path: str,
    database: str,
    approval_id: str,
    reviewer_grants: str,
    actor_grants: str,
) -> None:
    """Load the same keys and check the previous reviewer, actor and approval."""
    authority = ActorAuthority("host-app", key_path=key_path)
    reviewer = authority.verify(_read_actor(reviewer_path))
    actor = authority.verify(_read_actor(actor_path))
    if reviewer.actor_id == actor.actor_id:
        raise SystemExit(2)
    for grant in reviewer_grants.split(","):
        if not reviewer.has_grant(grant):
            raise SystemExit(3)
    for grant in actor_grants.split(","):
        if not actor.has_grant(grant):
            raise SystemExit(4)
    storage = SqliteStorage(database)
    try:
        approval = asyncio.run(storage.approvals.get(UUID(approval_id)))
    finally:
        storage.close()
    if approval is None or approval.status is not ApprovalStatus.GRANTED:
        raise SystemExit(5)
    if approval.decision_actor_id != reviewer.actor_id:
        raise SystemExit(6)
    print("verified")


def _read_actor(path: str) -> TrustedActor:
    return TrustedActor.from_credential(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    (
        key_path,
        reviewer_path,
        actor_path,
        database,
        approval_id,
        reviewer_grants,
        actor_grants,
    ) = sys.argv[1:]
    verify(
        key_path=key_path,
        reviewer_path=reviewer_path,
        actor_path=actor_path,
        database=database,
        approval_id=approval_id,
        reviewer_grants=reviewer_grants,
        actor_grants=actor_grants,
    )
