"""Alert backends — abstract base and concrete implementations."""

import logging
from abc import ABC, abstractmethod

import requests

logger = logging.getLogger(__name__)


class AlertBackend(ABC):
    """Abstract base class for alert delivery backends."""

    @abstractmethod
    def send(self, title: str, body: str) -> None:
        """Send an alert with the given title and body."""


class NtfyBackend(AlertBackend):
    """Alert backend that publishes to an ntfy topic."""

    def __init__(self, server: str, topic: str) -> None:
        self.server = server.rstrip("/")
        self.topic = topic

    def send(self, title: str, body: str) -> None:
        """POST a notification to the configured ntfy topic."""
        raise NotImplementedError
