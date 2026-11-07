"""Registry of capability providers (specification 5.7).

Not re-exported from ``id42_work.policy`` to avoid an import cycle with
``id42_work.contracts``; import it from ``id42_work`` or this module.
"""

from collections.abc import Iterator, Mapping
from types import MappingProxyType

from id42_work.contracts import CapabilityProvider
from id42_work.exceptions import WorkRegistrationError
from id42_work.policy.capabilities import CapabilitySpec


class CapabilityRegistry:
    """Maps each capability name to exactly one provider and its declared spec.

    Specs are read once, when the provider is added, so a provider cannot change what
    it declared (for example, stop declaring a capability as side-effecting) later.
    """

    def __init__(self) -> None:
        self._specs: dict[str, CapabilitySpec] = {}
        self._providers: dict[str, CapabilityProvider] = {}

    def add(self, provider: CapabilityProvider) -> None:
        """Register every capability ``provider`` declares.

        Raises ``WorkRegistrationError`` if a capability is already provided, if the
        provider declares the same capability twice, or declares nothing. The registry
        is unchanged when an error is raised.
        """
        specs = list(provider.capabilities())
        if not specs:
            msg = f"provider {type(provider).__name__} declares no capabilities"
            raise WorkRegistrationError(msg)
        names: set[str] = set()
        for spec in specs:
            if not isinstance(spec, CapabilitySpec):  # pyright: ignore[reportUnnecessaryIsInstance]
                msg = f"provider {type(provider).__name__} returned a non-CapabilitySpec value"
                raise WorkRegistrationError(msg)
            if spec.name in names:
                msg = f"provider declares capability {spec.name!r} twice"
                raise WorkRegistrationError(msg)
            if spec.name in self._specs:
                msg = f"capability {spec.name!r} is already provided"
                raise WorkRegistrationError(msg)
            names.add(spec.name)
        for spec in specs:
            self._specs[spec.name] = spec
            self._providers[spec.name] = provider

    def spec(self, name: str) -> CapabilitySpec | None:
        """The declared spec of ``name``, or ``None`` if no provider offers it."""
        return self._specs.get(name)

    def provider(self, name: str) -> CapabilityProvider | None:
        """The provider of ``name``, or ``None``."""
        return self._providers.get(name)

    def is_side_effecting(self, name: str) -> bool:
        """Whether ``name`` is declared side-effecting. Unknown names count as such."""
        spec = self._specs.get(name)
        return spec is None or spec.side_effecting

    @property
    def specs(self) -> Mapping[str, CapabilitySpec]:
        """Read-only view of every registered spec."""
        return MappingProxyType(self._specs)

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._specs))

    def __len__(self) -> int:
        return len(self._specs)
