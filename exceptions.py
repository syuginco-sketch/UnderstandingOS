class ClockError(Exception):
    """Base error for explicit CLOCK runtime failures."""


class ConfigurationError(ClockError):
    pass


class ContractValidationError(ClockError):
    pass


class VersionCompatibilityError(ContractValidationError):
    pass


class DependencyResolutionError(ClockError):
    pass


class LifecycleError(ClockError):
    pass


class PersistenceError(ClockError):
    pass


class EventBusError(ClockError):
    pass
