"""Policy models, capability declarations and policy resolvers.

``PolicyEngine`` and ``CapabilityRegistry`` depend on ``id42_work.contracts``; import
them from ``id42_work`` (or their modules) to avoid an import cycle.
"""

from id42_work.policy.capabilities import CapabilityCall, CapabilitySpec
from id42_work.policy.models import (
    ApprovalGate,
    ApprovalRequirement,
    AutonomyLevel,
    ExecutionLimits,
    Policy,
    PolicyDecision,
)
from id42_work.policy.resolvers import StaticPolicyResolver

__all__ = [
    "ApprovalGate",
    "ApprovalRequirement",
    "AutonomyLevel",
    "CapabilityCall",
    "CapabilitySpec",
    "ExecutionLimits",
    "Policy",
    "PolicyDecision",
    "StaticPolicyResolver",
]
