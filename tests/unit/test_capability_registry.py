import pytest

from _factories import StaticCapabilities
from id42_work import CapabilityRegistry, CapabilitySpec, WorkRegistrationError


def test_add_and_lookup() -> None:
    registry = CapabilityRegistry()
    provider = StaticCapabilities(("suppliers.read", False), ("odoo.invoice.post", True))
    registry.add(provider)
    assert "suppliers.read" in registry
    assert list(registry) == ["odoo.invoice.post", "suppliers.read"]
    assert len(registry) == 2
    assert registry.provider("suppliers.read") is provider
    assert registry.spec("odoo.invoice.post") == CapabilitySpec(
        name="odoo.invoice.post", side_effecting=True
    )
    assert registry.is_side_effecting("odoo.invoice.post")
    assert not registry.is_side_effecting("suppliers.read")
    assert registry.spec("unknown.cap") is None
    assert registry.provider("unknown.cap") is None


def test_unknown_capability_counts_as_side_effecting() -> None:
    assert CapabilityRegistry().is_side_effecting("payments.execute")


def test_duplicate_capability_across_providers_is_rejected_atomically() -> None:
    registry = CapabilityRegistry()
    registry.add(StaticCapabilities(("suppliers.read", False)))
    with pytest.raises(WorkRegistrationError, match="already provided"):
        registry.add(StaticCapabilities(("payments.execute", True), ("suppliers.read", False)))
    assert "payments.execute" not in registry


def test_invalid_providers() -> None:
    registry = CapabilityRegistry()
    with pytest.raises(WorkRegistrationError, match="no capabilities"):
        registry.add(StaticCapabilities())
    with pytest.raises(WorkRegistrationError, match="twice"):
        registry.add(StaticCapabilities(("a.read", False), ("a.read", True)))

    class BadProvider(StaticCapabilities):
        def capabilities(self) -> list[CapabilitySpec]:
            return ["a.read"]  # type: ignore[list-item]

    with pytest.raises(WorkRegistrationError, match="non-CapabilitySpec"):
        registry.add(BadProvider())
    assert len(registry) == 0


def test_specs_are_snapshotted_and_read_only() -> None:
    provider = StaticCapabilities(("odoo.invoice.post", True))
    registry = CapabilityRegistry()
    registry.add(provider)
    provider._specs.clear()  # provider changes its mind after registration
    assert registry.is_side_effecting("odoo.invoice.post")
    with pytest.raises(TypeError):
        registry.specs["x.read"] = CapabilitySpec(name="x.read", side_effecting=False)  # type: ignore[index]
