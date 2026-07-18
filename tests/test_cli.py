"""`permafrost` CLI surface (typer): replay, verify-chain, distill, report, bench, daemon guard."""

from __future__ import annotations

import json

from fastapi import FastAPI, Response
from typer.testing import CliRunner

from helpers import SEEDS_DIR, sql, flip_one_byte, write_csv
from permafrost.cli import app
from permafrost.rules import RuleBundleRejected
from permafrost.storage import Reading
from permafrost.timeutil import VIRTUAL_EPOCH_TS

runner = CliRunner()


def _door(db):
    return runner.invoke(app, ["replay", "--curve", str(SEEDS_DIR / "door_ajar.csv"), "--db", str(db), "--quiet"])


def test_replay_runs_and_reports(tmp_path):
    res = _door(tmp_path / "a.db")
    assert res.exit_code == 0
    assert "replay done" in res.output and "chain:" in res.output


def test_verify_chain_exit_zero_on_clean_db(tmp_path):
    db = tmp_path / "a.db"
    _door(db)
    res = runner.invoke(app, ["verify-chain", str(db)])
    assert res.exit_code == 0 and "OK" in res.output


def test_verify_chain_exit_one_on_tamper(tmp_path):
    db = tmp_path / "a.db"
    _door(db)
    (entry,) = sql(db, "SELECT entry FROM log_chain WHERE seq=10")[0]
    sql(db, "UPDATE log_chain SET entry=? WHERE seq=10", (flip_one_byte(entry),))
    res = runner.invoke(app, ["verify-chain", str(db)])
    assert res.exit_code == 1


def test_verify_chain_json_output(tmp_path):
    db = tmp_path / "a.db"
    _door(db)
    res = runner.invoke(app, ["verify-chain", str(db), "--json"])
    assert res.exit_code == 0
    assert json.loads(res.output.strip())["ok"] is True


def test_replay_offline_window(tmp_path):
    db = tmp_path / "a.db"
    res = runner.invoke(app, [
        "replay", "--curve", str(SEEDS_DIR / "door_ajar.csv"), "--db", str(db),
        "--offline-from", "1700", "--online-from", "2100", "--quiet",
    ])
    assert res.exit_code == 0 and "offline ticks" in res.output


def test_replay_without_quiet_prints_each_verdict(tmp_path):
    db = tmp_path / "a.db"
    res = runner.invoke(app, ["replay", "--curve", str(SEEDS_DIR / "door_ajar.csv"), "--db", str(db)])
    assert res.exit_code == 0
    assert "ExcursionVerdict" in res.output
    assert "cause      : door_ajar" in res.output
    assert "task id    :" in res.output


def test_replay_fresh_wipes_an_existing_db(tmp_path):
    db = tmp_path / "a.db"
    _door(db)
    n_before = sql(db, "SELECT COUNT(*) FROM log_chain")[0][0]
    res = runner.invoke(app, [
        "replay", "--curve", str(SEEDS_DIR / "door_ajar.csv"), "--db", str(db), "--quiet", "--fresh",
    ])
    assert res.exit_code == 0
    n_after = sql(db, "SELECT COUNT(*) FROM log_chain")[0][0]
    assert n_after == n_before  # --fresh wiped the db first, so the chain isn't doubled


def test_distill_after_replay(tmp_path):
    db = tmp_path / "a.db"
    runner.invoke(app, ["replay", "--curve", str(SEEDS_DIR / "defrost_cycle.csv"), "--db", str(db), "--quiet"])
    res = runner.invoke(app, ["distill", "--db", str(db)])
    assert res.exit_code == 0 and "Ed25519 signature" in res.output


def test_distill_activate_hotswaps(tmp_path):
    db = tmp_path / "a.db"
    runner.invoke(app, ["replay", "--curve", str(SEEDS_DIR / "defrost_cycle.csv"), "--db", str(db), "--quiet"])
    res = runner.invoke(app, ["distill", "--db", str(db), "--activate"])
    assert res.exit_code == 0 and "ACTIVATED" in res.output
    assert sql(db, "SELECT MAX(version) FROM rules")[0][0] == 2


def test_distill_writes_signed_bundle_to_file(tmp_path):
    db = tmp_path / "a.db"
    runner.invoke(app, ["replay", "--curve", str(SEEDS_DIR / "defrost_cycle.csv"), "--db", str(db), "--quiet"])
    out = tmp_path / "bundle.json"
    res = runner.invoke(app, ["distill", "--db", str(db), "--out", str(out)])
    assert res.exit_code == 0
    assert f"wrote {out}" in res.output
    data = json.loads(out.read_text())
    assert "bundle" in data and "sig" in data and "verify_key" in data


def test_distill_without_verdict_history_exits_one(tmp_path):
    # a curve with zero escalations produces zero verdicts to distill from
    csv = write_csv(tmp_path / "calm.csv", [(i * 10, 4.0, 45.0, 0, 1) for i in range(20)])
    db = tmp_path / "calm.db"
    assert runner.invoke(app, ["replay", "--curve", str(csv), "--db", str(db), "--quiet"]).exit_code == 0
    res = runner.invoke(app, ["distill", "--db", str(db)])
    assert res.exit_code == 1
    assert "no verdict history" in res.output


def test_distill_reports_cloud_failure(tmp_path, monkeypatch):
    import permafrost.cloud.app as cloud_app_module

    db = tmp_path / "a.db"
    runner.invoke(app, ["replay", "--curve", str(SEEDS_DIR / "defrost_cycle.csv"), "--db", str(db), "--quiet"])

    def _broken_create_app(*_a, **_kw):
        broken = FastAPI()

        @broken.post("/distill")
        async def _fail():
            return Response(status_code=500, content=b"boom")

        return broken

    monkeypatch.setattr(cloud_app_module, "create_app", _broken_create_app)
    res = runner.invoke(app, ["distill", "--db", str(db)])
    assert res.exit_code == 1
    assert "/distill failed" in res.output


def test_distill_activate_refusal_prints_message_and_exits_one(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    runner.invoke(app, ["replay", "--curve", str(SEEDS_DIR / "defrost_cycle.csv"), "--db", str(db), "--quiet"])

    def _boom(*_a, **_kw):
        raise RuleBundleRejected("forced refusal for coverage")

    monkeypatch.setattr("permafrost.daemon.activate_bundle_on_store", _boom)
    res = runner.invoke(app, ["distill", "--db", str(db), "--activate"])
    assert res.exit_code == 1
    assert "REFUSED" in res.output


# ── LIVE-mode transport failures (DEMO.md A7) ─────────────────────────────────
# The transport errors are MOCKED — no test ever makes a network call.


def _mock_status_error(status: int, code: str, request_id: str):
    import httpx
    from openai import APIStatusError, AuthenticationError, RateLimitError

    body = {
        "error": {"message": "Incorrect API key provided.", "type": "invalid_request_error", "code": code},
        "request_id": request_id,
    }
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"),
        json=body,
    )
    cls = {401: AuthenticationError, 429: RateLimitError}.get(status, APIStatusError)
    return cls(f"Error code: {status} - {body}", response=response, body=body)


def _replay_raising(monkeypatch, exc, tmp_path, live: bool = True):
    if live:
        monkeypatch.setenv("PERMAFROST_LIVE", "1")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-bogus")
    else:
        monkeypatch.delenv("PERMAFROST_LIVE", raising=False)

    def _raise(*_a, **_kw):
        raise exc

    monkeypatch.setattr("permafrost.replay.run_replay", _raise)
    return runner.invoke(app, [
        "replay", "--curve", str(SEEDS_DIR / "door_ajar.csv"), "--db", str(tmp_path / "live.db"), "--fresh",
    ])


def test_replay_live_auth_failure_renders_clean_card(tmp_path, monkeypatch):
    """A7: bogus key in live mode -> clean card, request_id preserved, exit 3, NO traceback."""
    res = _replay_raising(monkeypatch, _mock_status_error(401, "invalid_api_key", "b9fcd904-mock"), tmp_path)
    assert res.exit_code == 3
    assert "LIVE transport reached DashScope — authentication failed (invalid_api_key)" in res.output
    assert "b9fcd904-mock" in res.output  # the request_id is the proof the wiring is real
    assert "supply a valid DASHSCOPE_API_KEY" in res.output
    assert "Traceback" not in res.output and "AuthenticationError" not in res.output


def test_replay_live_non_401_status_renders_clean_card(tmp_path, monkeypatch):
    res = _replay_raising(monkeypatch, _mock_status_error(429, "rate_limit_exceeded", "req-429-mock"), tmp_path)
    assert res.exit_code == 3
    assert "HTTP 429 (rate_limit_exceeded)" in res.output
    assert "req-429-mock" in res.output
    assert "Traceback" not in res.output


def test_replay_live_connection_error_renders_clean_card(tmp_path, monkeypatch):
    import httpx
    from openai import APIConnectionError

    exc = APIConnectionError(
        request=httpx.Request("POST", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions")
    )
    res = _replay_raising(monkeypatch, exc, tmp_path)
    assert res.exit_code == 3
    assert "could not reach DashScope" in res.output
    assert "Traceback" not in res.output


def test_replay_live_mode_non_api_error_still_propagates(tmp_path, monkeypatch):
    """The card must never swallow a non-transport bug, even in live mode."""
    res = _replay_raising(monkeypatch, ValueError("not a transport error"), tmp_path)
    assert res.exit_code != 0
    assert isinstance(res.exception, ValueError)


def test_replay_offline_errors_still_propagate_unchanged(tmp_path, monkeypatch):
    """Without PERMAFROST_LIVE the wrapper is inert: exceptions re-raise as before."""
    res = _replay_raising(monkeypatch, _mock_status_error(401, "invalid_api_key", "req-x"), tmp_path, live=False)
    assert res.exit_code != 0
    assert res.exception is not None and "Error code: 401" in str(res.exception)


def test_report_week(tmp_path):
    db = tmp_path / "a.db"
    _door(db)
    res = runner.invoke(app, ["report", "--week", "2", "--db", str(db)])
    assert res.exit_code == 0 and "compliance report" in res.output


def test_report_writes_to_file(tmp_path):
    db = tmp_path / "a.db"
    _door(db)
    out = tmp_path / "report.md"
    res = runner.invoke(app, ["report", "--week", "2", "--db", str(db), "--out", str(out)])
    assert res.exit_code == 0
    assert f"wrote {out}" in res.output
    assert "compliance report" in out.read_text()


def test_bench_quick(tmp_path):
    res = runner.invoke(app, ["bench", "--seeds", str(SEEDS_DIR), "--workdir", str(tmp_path / "b"), "--quick"])
    assert res.exit_code == 0 and "accuracy" in res.output and "PASS" in res.output


def test_bench_missing_seeds_dir_exits_two(tmp_path):
    res = runner.invoke(app, ["bench", "--seeds", str(tmp_path / "nope"), "--workdir", str(tmp_path / "b")])
    assert res.exit_code == 2
    assert "seeds dir not found" in res.output


def test_bench_writes_report_to_file(tmp_path):
    out = tmp_path / "bench.md"
    res = runner.invoke(app, [
        "bench", "--seeds", str(SEEDS_DIR), "--workdir", str(tmp_path / "b"), "--quick", "--out", str(out),
    ])
    assert res.exit_code == 0
    assert f"wrote {out}" in res.output
    assert "accuracy" in out.read_text()


def test_daemon_without_hardware_exits_two(tmp_path):
    res = runner.invoke(app, ["daemon", "--db", str(tmp_path / "d.db")])
    assert res.exit_code == 2  # GpioSource -> HardwareUnavailable, guarded


def test_daemon_processes_one_tick_then_stops_on_interrupt(tmp_path, monkeypatch):
    """A fake hardware source that hands back one real sample lets the daemon loop
    actually run (sense -> reflex -> log) before we ctrl-C it via a mocked sleep."""

    class _FakeGpioSource:
        def __init__(self, door_pin=17, power_pin=27):
            self.door_pin, self.power_pin = door_pin, power_pin

        def read(self):
            return Reading(ts=VIRTUAL_EPOCH_TS, temp_c=4.0, humidity_pct=45.0, door_open=False, power_ok=True)

    def _interrupt(*_a, **_kw):
        raise KeyboardInterrupt

    monkeypatch.setattr("permafrost.sampler.GpioSource", _FakeGpioSource)
    monkeypatch.setattr("time.sleep", _interrupt)
    res = runner.invoke(app, ["daemon", "--db", str(tmp_path / "hw.db")])
    assert res.exit_code == 0
    assert "permafrost daemon:" in res.output
    assert "stopped." in res.output


def test_daemon_with_cloud_url_constructs_diagnoser_offline(tmp_path, monkeypatch):
    """--cloud-url wires an HttpLink; this must never make a real network call in
    tests, so the fake source reports nothing sampled yet and we interrupt before
    process_tick ever runs — only the diagnoser CONSTRUCTION path is under test."""

    class _FakeGpioSourceNoSample:
        def __init__(self, door_pin=17, power_pin=27):
            pass

        def read(self):
            return None

    def _interrupt(*_a, **_kw):
        raise KeyboardInterrupt

    monkeypatch.setattr("permafrost.sampler.GpioSource", _FakeGpioSourceNoSample)
    monkeypatch.setattr("time.sleep", _interrupt)
    res = runner.invoke(app, [
        "daemon", "--db", str(tmp_path / "hw2.db"), "--cloud-url", "http://cloud.example.invalid",
    ])
    assert res.exit_code == 0
    assert "stopped." in res.output


def test_no_args_shows_help():
    res = runner.invoke(app, [])
    assert res.exit_code == 2  # no_args_is_help
    assert "replay" in res.output and "verify-chain" in res.output
