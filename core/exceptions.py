from enum import Enum
from dataclasses import dataclass
from typing import Optional


class ErrorSeverity(Enum):
    TRANSIENT = "transient"      # retryable, log at WARNING
    DEGRADED = "degraded"        # module continues in reduced mode
    FATAL = "fatal"              # orchestrator should isolate module


@dataclass
class PassiveVigilanceError(Exception):
    module: str
    severity: ErrorSeverity
    message: str
    original_exception: Optional[Exception] = None
    context: dict = None

    def __str__(self):
        return f"[{self.module}] {self.severity.value.upper()}: {self.message}"


class SensorInitError(PassiveVigilanceError): ...
class SensorReadError(PassiveVigilanceError): ...
class GPSFixLostError(PassiveVigilanceError): ...
class AlertDeliveryError(PassiveVigilanceError): ...
class RemoteIDParseError(PassiveVigilanceError): ...
