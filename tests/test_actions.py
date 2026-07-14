"""Action sinks + dispatcher routing (actions.py): buzzer/notifier stubs + the tool router."""

from __future__ import annotations

from permafrost.actions import ActionDispatcher, BuzzerSink, NotifierSink


def test_dispatch_sound_alarm_routes_to_buzzer():
    buzzer, notifier = BuzzerSink(), NotifierSink()
    d = ActionDispatcher(buzzer, notifier)
    assert d.dispatch(10.0, "sound_alarm", "reflex", {"rule_id": "hi"}) == "buzzer"
    assert len(buzzer.activations) == 1
    assert buzzer.activations[0].detail == {"rule_id": "hi"}
    assert not notifier.notifications


def test_dispatch_notify_routes_to_notifier():
    buzzer, notifier = BuzzerSink(), NotifierSink()
    d = ActionDispatcher(buzzer, notifier)
    assert d.dispatch(10.0, "notify", "verdict", {"channel": "phone"}) == "notifier"
    assert notifier.notifications[0].detail["channel"] == "phone"
    assert not buzzer.activations


def test_dispatch_schedule_service_forces_service_channel():
    buzzer, notifier = BuzzerSink(), NotifierSink()
    d = ActionDispatcher(buzzer, notifier)
    assert d.dispatch(10.0, "schedule_service", "verdict", {"note": "inspect gasket"}) == "notifier"
    detail = notifier.notifications[0].detail
    assert detail["channel"] == "service" and detail["note"] == "inspect gasket"


def test_dispatch_unknown_tool_is_recorded_not_raised():
    buzzer, notifier = BuzzerSink(), NotifierSink()
    d = ActionDispatcher(buzzer, notifier)
    result = d.dispatch(10.0, "launch_missile", "verdict", {"note": "should never happen"})
    assert result == "unknown"
    assert len(d.unknown) == 1
    assert d.unknown[0].tool == "launch_missile" and d.unknown[0].source == "verdict"
    assert not buzzer.activations and not notifier.notifications


def test_buzzer_sink_records_activations_without_gpio():
    buzzer = BuzzerSink()  # gpio_pin=None -> stub only, no hardware attempted
    buzzer.sound(0.0, "reflex", {"rule_id": "door"})
    assert buzzer.activations[0].tool == "sound_alarm"
