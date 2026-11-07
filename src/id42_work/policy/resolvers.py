"""Policy resolvers shipped with the SDK (specification 6.1)."""

from collections.abc import Mapping
from types import MappingProxyType

from id42_work.actors import TrustedActor
from id42_work.definitions import Work
from id42_work.policy.models import Policy

PolicyKey = tuple[str, str]
"""``(work_id, work_version)``."""

TenantPolicyKey = tuple[str, str, str]
"""``(tenant_id, work_id, work_version)``."""


class StaticPolicyResolver:
    """Resolves policies from fixed mappings configured by the host.

    A tenant-specific policy, when present for the actor's tenant, replaces the default
    policy of that work version. A work version without a policy resolves to ``None``,
    which denies execution. Mappings are copied at construction.
    """

    def __init__(
        self,
        policies: Mapping[PolicyKey, Policy],
        *,
        tenant_policies: Mapping[TenantPolicyKey, Policy] | None = None,
    ) -> None:
        self._policies: Mapping[PolicyKey, Policy] = MappingProxyType(dict(policies))
        self._tenant_policies: Mapping[TenantPolicyKey, Policy] = MappingProxyType(
            dict(tenant_policies or {})
        )

    def resolve(self, work: Work, actor: TrustedActor) -> Policy | None:
        """Return the policy for ``work`` as seen by ``actor``'s tenant, or ``None``."""
        if actor.tenant_id is not None:
            tenant_policy = self._tenant_policies.get((actor.tenant_id, work.id, work.version))
            if tenant_policy is not None:
                return tenant_policy
        return self._policies.get((work.id, work.version))
