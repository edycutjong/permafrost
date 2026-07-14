"""Session fixtures: each seed curve is replayed exactly once and shared.

Everything here is offline and keyless: transports are FakeQwen, links are
in-process ASGI, clocks are virtual.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import SEEDS_DIR
from permafrost.crypto import dev_keys
from permafrost.qwen.fake import FakeQwen
from permafrost.replay import ReplayResult, run_replay


@pytest.fixture(scope="session")
def seeds_dir() -> Path:
    return SEEDS_DIR


@pytest.fixture(scope="session")
def keys():
    return dev_keys()


def _replay(tmp_root: Path, curve: str) -> tuple[ReplayResult, Path, FakeQwen]:
    db = tmp_root / f"{curve}.db"
    transport = FakeQwen()
    result = run_replay(SEEDS_DIR / f"{curve}.csv", db, transport=transport)
    return result, db, transport


@pytest.fixture(scope="session")
def door_replay(tmp_path_factory) -> tuple[ReplayResult, Path, FakeQwen]:
    return _replay(tmp_path_factory.mktemp("door"), "door_ajar")


@pytest.fixture(scope="session")
def defrost_replay(tmp_path_factory) -> tuple[ReplayResult, Path, FakeQwen]:
    return _replay(tmp_path_factory.mktemp("defrost"), "defrost_cycle")


@pytest.fixture(scope="session")
def power_replay(tmp_path_factory) -> tuple[ReplayResult, Path, FakeQwen]:
    return _replay(tmp_path_factory.mktemp("power"), "power_loss")


@pytest.fixture(scope="session")
def compressor_replay(tmp_path_factory) -> tuple[ReplayResult, Path, FakeQwen]:
    return _replay(tmp_path_factory.mktemp("compressor"), "compressor_drift")


@pytest.fixture(scope="session")
def offline_door_replay(tmp_path_factory) -> tuple[ReplayResult, Path, FakeQwen]:
    """Door curve with the network cut before the door opens and restored later."""
    tmp = tmp_path_factory.mktemp("offline")
    db = tmp / "door_offline.db"
    transport = FakeQwen()
    result = run_replay(
        SEEDS_DIR / "door_ajar.csv",
        db,
        transport=transport,
        offline_from=1700,
        online_from=2100,
    )
    return result, db, transport


@pytest.fixture(scope="session")
def bench_results(tmp_path_factory):
    from permafrost.benchmark import run_all

    md, ok, data = run_all(SEEDS_DIR, tmp_path_factory.mktemp("bench"), quick=True)
    return md, ok, data
