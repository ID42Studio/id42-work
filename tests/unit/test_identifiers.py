import pytest
from pydantic import TypeAdapter, ValidationError

from id42_work.identifiers import (
    ActionId,
    Capability,
    Grant,
    Identifier,
    SemVer,
    approve_grant,
    is_capability,
    reconcile_grant,
    resume_grant,
)


@pytest.mark.parametrize(
    "version", ["0.1.0", "1.0.0", "10.20.30", "1.0.0-rc.1", "1.0.0-alpha+build.5", "1.0.0+20261106"]
)
def test_valid_semver(version: str) -> None:
    assert TypeAdapter(SemVer).validate_python(version) == version


@pytest.mark.parametrize("version", ["1", "1.0", "01.0.0", "1.0.0-", "v1.0.0", "1.0.0.0", ""])
def test_invalid_semver(version: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(SemVer).validate_python(version)


@pytest.mark.parametrize("name", ["documents.read", "odoo.invoice.post", "payments.execute", "a"])
def test_valid_capabilities(name: str) -> None:
    assert TypeAdapter(Capability).validate_python(name) == name
    assert is_capability(name)


@pytest.mark.parametrize(
    "name", ["", "Odoo.read", "odoo..read", ".read", "read.", "approve:x", "a b", "x" * 129]
)
def test_invalid_capabilities(name: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Capability).validate_python(name)
    assert not is_capability(name)


def test_grants_accept_capabilities_and_prefixed_targets() -> None:
    adapter = TypeAdapter(Grant)
    for grant in (
        "odoo.invoice.post",
        approve_grant("payments.execute"),
        resume_grant("w"),
        reconcile_grant("note"),
        reconcile_grant("a" * 128),
    ):
        assert adapter.validate_python(grant) == grant
    for bad in ("admin:all", "approve:", "approve:Bad"):
        with pytest.raises(ValidationError):
            adapter.validate_python(bad)


def test_identifier_and_action_id() -> None:
    assert TypeAdapter(Identifier).validate_python("supplier_invoice_validation")
    assert TypeAdapter(ActionId).validate_python("post-invoice:1")
    with pytest.raises(ValidationError):
        TypeAdapter(ActionId).validate_python("__start__")  # reserved for gates
