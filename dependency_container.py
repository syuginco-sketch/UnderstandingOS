from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from runtime.exceptions import DependencyResolutionError
from runtime.protocols import ShutdownPriority


class DependencyContainer:
    shutdown_priority = ShutdownPriority.DEFAULT

    def __init__(self) -> None:
        self._providers: dict[str, tuple[object, tuple[str, ...]]] = {}
        self._instances: dict[str, object] = {}
        self._resolving: list[str] = []
        self.state = "INITIALIZED"

    def register(
        self,
        name: str,
        provider: object,
        dependencies: Iterable[str] = (),
    ) -> None:
        if not name or not isinstance(name, str):
            raise DependencyResolutionError("Service name must be a non-empty string")
        if name in self._providers:
            raise DependencyResolutionError(f"Service is already registered: {name}")
        self._providers[name] = (provider, tuple(dependencies))

    def resolve(self, name: str) -> object:
        if name in self._instances:
            return self._instances[name]
        if name not in self._providers:
            raise DependencyResolutionError(f"Service is not registered: {name}")
        if name in self._resolving:
            cycle = " -> ".join((*self._resolving, name))
            raise DependencyResolutionError(f"Circular dependency detected: {cycle}")

        provider, dependencies = self._providers[name]
        self._resolving.append(name)
        try:
            arguments = [self.resolve(dependency) for dependency in dependencies]
            if callable(provider):
                instance = provider(*arguments)
            elif dependencies:
                raise DependencyResolutionError(
                    f"Registered instance {name} cannot receive dependencies"
                )
            else:
                instance = provider
        except DependencyResolutionError:
            raise
        except Exception as error:
            raise DependencyResolutionError(f"Unable to construct service {name}: {error}") from error
        finally:
            self._resolving.pop()
        self._instances[name] = instance
        return instance

    def inject(self, target: object, **attribute_to_service: str) -> object:
        for attribute, service_name in attribute_to_service.items():
            setattr(target, attribute, self.resolve(service_name))
        return target

    def validate(self) -> None:
        for name, (_, dependencies) in self._providers.items():
            for dependency in dependencies:
                if dependency not in self._providers:
                    raise DependencyResolutionError(
                        f"Service {name} depends on unregistered service {dependency}"
                    )

        resolved: set[str] = set()

        def visit(name: str) -> None:
            if name in resolved:
                return
            if name in self._resolving:
                cycle = " -> ".join((*self._resolving, name))
                raise DependencyResolutionError(f"Circular dependency detected: {cycle}")
            self._resolving.append(name)
            try:
                for dependency in self._providers[name][1]:
                    visit(dependency)
            finally:
                self._resolving.pop()
            resolved.add(name)

        for name in self._providers:
            visit(name)

    def initialize(self) -> None:
        self.validate()

    def start(self) -> None:
        self.validate()
        self.state = "RUNNING"

    def stop(self) -> None:
        self._instances.clear()
        self.state = "STOPPED"

    def health(self) -> dict[str, str]:
        return {"service": "DependencyContainer", "state": self.state}
