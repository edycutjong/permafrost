"""Action sinks: buzzer / notifier stubs (SPEC scope: "mock the notifier").

In replay mode the stubs record every activation (tests assert on them and
the CLI prints them). On a Pi, ``BuzzerSink`` drives a GPIO pin when the
optional hardware stack is present — imports stay guarded so replay never
touches GPIO.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["Activation", "BuzzerSink", "NotifierSink", "ActionDispatcher"]


@dataclass(frozen=True)
class Activation:
    ts: float
    tool: str
    source: str  # "reflex" | "verdict"
    detail: dict[str, Any]


class BuzzerSink:
    """Piezo buzzer stub; optionally drives GPIO when available (hardware mode)."""

    def __init__(self, gpio_pin: int | None = None):
        self.activations: list[Activation] = []
        self._gpio = None
        if gpio_pin is not None:  # pragma: no cover - requires physical rig
            try:
                from gpiozero import Buzzer  # type: ignore[import-not-found]

                self._gpio = Buzzer(gpio_pin)
            except ImportError:
                self._gpio = None  # stub-only; recorded activations still work

    def sound(self, ts: float, source: str, detail: dict[str, Any]) -> None:
        self.activations.append(Activation(ts, "sound_alarm", source, detail))
        if self._gpio is not None:  # pragma: no cover
            self._gpio.beep(on_time=0.5, off_time=0.2, n=3, background=True)


class NotifierSink:
    """Mock SMS/phone/service notifier (provider integration is OUT of scope)."""

    def __init__(self) -> None:
        self.notifications: list[Activation] = []

    def notify(self, ts: float, source: str, detail: dict[str, Any]) -> None:
        self.notifications.append(Activation(ts, "notify", source, detail))


class ActionDispatcher:
    """Routes typed action names (reflex rules + verdict tool calls) to sinks.

    ``annotate_log`` and ``update_edge_rules`` are handled by the daemon itself
    (they touch the chain / bundle state); the dispatcher records everything
    else and never raises on unknown tools — it logs them as anomalies.
    """

    def __init__(self, buzzer: BuzzerSink, notifier: NotifierSink):
        self.buzzer = buzzer
        self.notifier = notifier
        self.unknown: list[Activation] = []

    def dispatch(self, ts: float, tool: str, source: str, detail: dict[str, Any]) -> str:
        if tool == "sound_alarm":
            self.buzzer.sound(ts, source, detail)
            return "buzzer"
        if tool == "notify":
            self.notifier.notify(ts, source, detail)
            return "notifier"
        if tool == "schedule_service":
            self.notifier.notify(ts, source, {**detail, "channel": "service"})
            return "notifier"
        self.unknown.append(Activation(ts, tool, source, detail))
        return "unknown"
