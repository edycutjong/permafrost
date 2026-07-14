"""``permafrost`` CLI — daemon · replay · verify-chain · distill · report · bench."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(
    name="permafrost",
    help="The vaccine fridge that argues for its own contents.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


def _echo_verdict(envelope: dict) -> None:
    v = envelope.get("verdict", {})
    color = typer.colors.BLUE if v.get("benign") else typer.colors.RED
    tag = "BENIGN" if v.get("benign") else "EXCURSION"
    typer.secho(
        f"\n┌─ ExcursionVerdict [{tag}] ────────────────────────────", fg=color, bold=True
    )
    typer.secho(f"│ cause      : {v.get('cause')}  (confidence {v.get('confidence')})", fg=color)
    risk = v.get("risk") or {}
    if risk.get("stock_at_risk_in_min") is not None:
        typer.secho(f"│ risk       : stock at risk in {risk['stock_at_risk_in_min']} min "
                    f"({risk.get('vfc_grade_impact') or 'grade impact n/a'})", fg=color)
    for e in v.get("evidence", []):
        typer.secho(f"│ evidence   : {e}", fg=color)
    typer.secho(f"│ guidance   : {str(v.get('guidance_citation'))[:110]}", fg=color)
    actions = ", ".join(a.get("tool", "?") for a in v.get("actions", []))
    typer.secho(f"│ actions    : {actions}", fg=color)
    typer.secho(f"│ task id    : {envelope.get('task_id')}  model: {envelope.get('model')}", fg=color)
    typer.secho("└──────────────────────────────────────────────────────", fg=color)


@app.command()
def daemon(
    db: Path = typer.Option(Path("audit.db"), help="Audit database path (SQLite WAL)."),
    door_pin: int = typer.Option(17, help="Reed-switch GPIO pin."),
    power_pin: int = typer.Option(27, help="Mains-sense GPIO pin."),
    buzzer_pin: Optional[int] = typer.Option(18, help="Piezo buzzer GPIO pin."),
    cloud_url: Optional[str] = typer.Option(None, help="Deployed cloud base URL (Function Compute)."),
) -> None:
    """Run the edge daemon on real hardware (Raspberry Pi; GPIO imports are guarded).

    No hardware? The identical loop runs from recorded curves:
    ``permafrost replay --curve seeds/door_ajar.csv``
    """
    import time as _time

    from .actions import BuzzerSink
    from .crypto import dev_keys
    from .daemon import EdgeDaemon
    from .link import DiagnoserClient, HttpLink
    from .sampler import SAMPLE_PERIOD_S, GpioSource, HardwareUnavailable
    from .storage import EdgeStore

    try:
        source = GpioSource(door_pin=door_pin, power_pin=power_pin)
    except HardwareUnavailable as exc:
        typer.secho(f"hardware unavailable: {exc}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=2) from exc

    diagnoser = None
    if cloud_url:
        diagnoser = DiagnoserClient(HttpLink(cloud_url), dev_keys().sealing_public)
    store = EdgeStore(db)
    d = EdgeDaemon(store, diagnoser=diagnoser, buzzer=BuzzerSink(gpio_pin=buzzer_pin))
    typer.echo(f"permafrost daemon: sampling every {SAMPLE_PERIOD_S:.0f}s -> {db} (rules v{d.rules_version})")
    try:
        while True:  # pragma: no cover - hardware loop
            sample = source.read()
            if sample is not None:
                result = d.process_tick(sample)
                for envelope in result.verdicts:
                    _echo_verdict(envelope)
            _time.sleep(SAMPLE_PERIOD_S)
    except KeyboardInterrupt:  # pragma: no cover
        typer.echo("stopped.")
    finally:
        store.close()


@app.command()
def replay(
    curve: Path = typer.Option(..., exists=True, dir_okay=False, help="Seed curve CSV, e.g. seeds/door_ajar.csv"),
    db: Path = typer.Option(Path("audit.db"), help="Audit database path."),
    offline_from: Optional[int] = typer.Option(None, help="Cut the (virtual) network at this tick."),
    online_from: Optional[int] = typer.Option(None, help="Restore the network at this tick."),
    tick_limit: Optional[int] = typer.Option(None, help="Stop after N ticks (power-cut harness)."),
    throttle_ms: float = typer.Option(0.0, help="Sleep per tick (slows replay for kill demos)."),
    resume: bool = typer.Option(False, "--resume", help="Continue a killed replay from the last committed reading."),
    fresh: bool = typer.Option(False, "--fresh", help="Delete the db first."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress tick narration."),
) -> None:
    """Replay a recorded curve through the FULL loop — the zero-hardware judging path."""
    from .replay import run_replay

    if fresh:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)

    verdicts_seen: list[dict] = []

    result = run_replay(
        curve,
        db,
        offline_from=offline_from,
        online_from=online_from,
        tick_limit=tick_limit,
        throttle_ms=throttle_ms,
        resume=resume,
        verbose_print=(None if quiet else lambda msg: typer.echo(msg)),
    )
    for envelope in result.verdicts:
        if not quiet:
            _echo_verdict(envelope)
        verdicts_seen.append(envelope)

    typer.echo(
        f"\nreplay done: {result.ticks} ticks | reflex firings {len(result.firings)} | "
        f"alarms {len(result.alarms)} | verdicts {len(result.verdicts)} | "
        f"offline ticks {result.offline_ticks} | queue pending {result.pending_after} "
        f"(synced {result.synced_total}) | rules v{result.rules_version}"
    )
    if result.chain_report is not None:
        ok = result.chain_report.ok and result.chain_report.roots_ok
        typer.secho(
            f"chain: {result.chain_report.summary()}",
            fg=typer.colors.GREEN if ok else typer.colors.RED,
        )
    typer.echo(f"audit db: {db} — verify anytime with: permafrost verify-chain {db}")


@app.command(name="verify-chain")
def verify_chain_cmd(
    db: Path = typer.Argument(..., exists=True, help="Audit database to verify."),
    verify_key: Optional[str] = typer.Option(None, help="Ed25519 verify key hex (default: dev demo key)."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Re-derive the whole hash chain + signed daily Merkle roots. Exit 1 on tamper."""
    from .chain import verify_chain
    from .crypto import dev_keys

    report = verify_chain(db, verify_key or dev_keys().verify_key)
    ok = report.ok and report.roots_ok
    if as_json:
        typer.echo(json.dumps({
            "ok": ok, "entries": report.entries, "first_bad_seq": report.first_bad_seq,
            "reason": report.reason, "roots_checked": report.roots_checked,
            "root_failures": report.root_failures,
        }))
    else:
        typer.secho(report.summary(), fg=typer.colors.GREEN if ok else typer.colors.RED, bold=True)
    raise typer.Exit(code=0 if ok else 1)


@app.command()
def distill(
    db: Path = typer.Option(..., exists=True, help="Audit db whose verdict history to distill."),
    out: Optional[Path] = typer.Option(None, help="Write the signed bundle JSON here."),
    activate: bool = typer.Option(False, "--activate", help="Verify signature and hot-swap into the edge db."),
) -> None:
    """Cloud-compile verdict history into a signed local rule bundle (qwen3.6-flash)."""
    from .chain import ChainLogger
    from .cloud.app import create_app
    from .daemon import activate_bundle_on_store
    from .crypto import dev_keys
    from .reporting import verdict_history
    from .rules import RuleBundle, RuleBundleRejected
    from .storage import EdgeStore

    history = verdict_history(db)
    if not history:
        typer.secho("no verdict history in this db — replay a curve first.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    store = EdgeStore(db)
    try:
        active = store.active_rules()
        current_version = active[0] if active else 1
        now_ts = max(v["ts"] for v in history)

        from .link import make_inprocess_client

        cloud = create_app()
        client = make_inprocess_client(cloud, base_url="http://cloud.local")
        resp = client.post("/distill", json={
            "verdicts": [{k: v for k, v in row.items() if k not in ("ts", "task_id")} for row in history],
            "current_version": current_version,
            "now_ts": now_ts,
        })
        client.close()
        if resp.status_code != 200:
            typer.secho(f"/distill failed: {resp.status_code} {resp.text}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        result = resp.json()

        bundle = result["bundle"]
        typer.echo(RuleBundle.parse(bundle).if_then_text())
        typer.echo(f"\nEd25519 signature: {result['sig'][:32]}…  (verify key {result['verify_key'][:16]}…)")
        if out:
            out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
            typer.echo(f"wrote {out}")

        if activate:
            chain = ChainLogger(store)
            try:
                activate_bundle_on_store(store, chain, bundle, result["sig"], dev_keys().verify_key, now_ts)
                typer.secho(
                    f"ACTIVATED rules v{bundle['version']} (signature verified before hot-swap)",
                    fg=typer.colors.GREEN, bold=True,
                )
            except RuleBundleRejected as exc:
                typer.secho(f"REFUSED: {exc}", fg=typer.colors.RED, bold=True)
                raise typer.Exit(code=1) from exc
    finally:
        store.close()


@app.command()
def report(
    week: int = typer.Option(..., min=1, max=53, help="ISO week number (replay curves live in week 2)."),
    db: Path = typer.Option(..., exists=True, help="Audit database."),
    out: Optional[Path] = typer.Option(None, help="Write markdown here instead of stdout."),
) -> None:
    """Weekly VFC-style compliance report mined from the tamper-evident chain."""
    from .reporting import edge_weekly_report

    md = edge_weekly_report(db, week)
    if out:
        out.write_text(md + "\n")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(md)


@app.command()
def bench(
    seeds: Path = typer.Option(Path("seeds"), help="Seed curves directory."),
    workdir: Path = typer.Option(Path("out/bench"), help="Scratch dir for bench replays."),
    quick: bool = typer.Option(False, "--quick", help="Fewer runs (CI smoke)."),
    out: Optional[Path] = typer.Option(None, help="Write the markdown report here."),
) -> None:
    """Confusion matrix + reflex latency + $/day pre/post-distill (exit 1 if any floor fails)."""
    from .benchmark import run_all

    if not seeds.exists():
        typer.secho(f"seeds dir not found: {seeds}", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    md, ok, _ = run_all(seeds, workdir, quick=quick)
    if out:
        out.write_text(md + "\n")
        typer.echo(f"wrote {out}")
    typer.echo(md)
    raise typer.Exit(code=0 if ok else 1)


if __name__ == "__main__":  # pragma: no cover
    app()
