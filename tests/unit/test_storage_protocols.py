from uuid import uuid4

import pytest
from _factories import DIGEST_A
from pydantic import ValidationError

from id42_work import storage
from id42_work.storage import IdempotencyRecord, IdempotencyScope


def test_storage_protocols_are_exported() -> None:
    assert set(storage.__all__) == {
        "ApprovalRepository",
        "EventRepository",
        "EvidenceRepository",
        "ExecutionRepository",
        "IdempotencyRecord",
        "IdempotencyRepository",
        "IdempotencyScope",
        "InputStore",
    }


def test_idempotency_scope_includes_tenant_and_version() -> None:
    a = IdempotencyScope(tenant_id="acme", work_id="w", work_version="1.0.0", idempotency_key="k")
    assert a != a.model_copy(update={"tenant_id": "other"})
    assert a != a.model_copy(update={"work_version": "1.0.1"})
    assert hash(a) == hash(IdempotencyScope.model_validate(a.model_dump()))
    record = IdempotencyRecord(scope=a, input_fingerprint=DIGEST_A, execution_id=uuid4())
    assert record.scope.idempotency_key == "k"
    with pytest.raises(ValidationError):
        IdempotencyScope(tenant_id=None, work_id="w", work_version="1.0.0", idempotency_key="")
