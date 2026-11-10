from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Mapping

from runtime.exceptions import ConfigurationError
from runtime.protocols import ShutdownPriority


DEFAULT_CONFIG = {
    "runtime": {
        "log_level": "INFO",
        "health_interval_seconds": 30,
    }
}
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class ConfigurationLoader:
    shutdown_priority = ShutdownPriority.DEFAULT

    def __init__(
        self,
        config_path: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.config_path = Path(config_path) if config_path is not None else Path("config.json")
        self.environment = environment if environment is not None else os.environ
        self.configuration = copy.deepcopy(DEFAULT_CONFIG)
        self.state = "INITIALIZED"

    def load(self) -> dict:
        configuration = copy.deepcopy(DEFAULT_CONFIG)
        try:
            if self.config_path.exists():
                with self.config_path.open("r", encoding="utf-8") as file:
                    file_configuration = json.load(file)
                self._merge_file_configuration(configuration, file_configuration)

            self._apply_environment(configuration)
            self._validate(configuration)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            self.state = "FAILED"
            raise ConfigurationError(f"Unable to load configuration: {error}") from error
        except ConfigurationError:
            self.state = "FAILED"
            raise

        self.configuration = configuration
        self.state = "INITIALIZED"
        return copy.deepcopy(configuration)

    def initialize(self) -> None:
        self.load()

    def start(self) -> None:
        self.state = "RUNNING"

    def stop(self) -> None:
        self.state = "STOPPED"

    def health(self) -> dict[str, str]:
        return {"service": "ConfigurationLoader", "state": self.state}

    def get(self, key: str) -> object:
        if key == "runtime":
            return copy.deepcopy(self.configuration["runtime"])
        if key.startswith("runtime."):
            part = key.partition(".")[2]
            if part in self.configuration["runtime"]:
                return self.configuration["runtime"][part]
        raise ConfigurationError(f"Unknown configuration key: {key}")

    @staticmethod
    def _merge_file_configuration(target: dict, source: object) -> None:
        if not isinstance(source, dict):
            raise ConfigurationError("Configuration root must be a JSON object")
        unknown = set(source) - {"runtime"}
        if unknown:
            raise ConfigurationError(f"Unknown configuration sections: {sorted(unknown)}")
        if "runtime" not in source:
            return
        runtime = source["runtime"]
        if not isinstance(runtime, dict):
            raise ConfigurationError("The runtime configuration must be a JSON object")
        unknown_runtime = set(runtime) - {"log_level", "health_interval_seconds"}
        if unknown_runtime:
            raise ConfigurationError(f"Unknown runtime settings: {sorted(unknown_runtime)}")
        target["runtime"].update(runtime)

    def _apply_environment(self, configuration: dict) -> None:
        if "DG_LOG_LEVEL" in self.environment:
            configuration["runtime"]["log_level"] = self.environment["DG_LOG_LEVEL"]
        if "DG_HEALTH_INTERVAL_SECONDS" in self.environment:
            raw_interval = self.environment["DG_HEALTH_INTERVAL_SECONDS"]
            try:
                configuration["runtime"]["health_interval_seconds"] = int(raw_interval)
            except (TypeError, ValueError) as error:
                raise ConfigurationError(
                    "DG_HEALTH_INTERVAL_SECONDS must be an integer"
                ) from error

    @staticmethod
    def _validate(configuration: dict) -> None:
        runtime = configuration["runtime"]
        level = runtime.get("log_level")
        if not isinstance(level, str) or level.upper() not in LOG_LEVELS:
            raise ConfigurationError(
                f"log_level must be one of {', '.join(sorted(LOG_LEVELS))}"
            )
        runtime["log_level"] = level.upper()

        interval = runtime.get("health_interval_seconds")
        if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
            raise ConfigurationError("health_interval_seconds must be an integer greater than 0")
