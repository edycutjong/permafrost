"""Cloud /diagnose pipeline: feature classifier, fixture verdicts, embeddings, RAG citation."""

from __future__ import annotations

import json

import pytest

from helpers import SEEDS_DIR
from permafrost.cloud.diagnose import DiagnoseRequest, diagnose
from permafrost.cloud.guidance import GuidanceStore, Snippet, _cosine, format_citation
from permafrost.qwen.fake import FakeQwen, classify_features, fixture_verdict, hash_embed
from permafrost.qwen.transport import ALLOWED_MODELS, MODEL_DIAGNOSIS, MODEL_TTS, ChatResult, UsageMeter
from permafrost.sampler import CsvSource
from permafrost.verdict import ExcursionVerdict
from permafrost.timeutil import day_of


class _StubTransport:
    """Returns fixed ``chat`` content — used to force diagnose()'s two error paths
    (non-JSON model output, schema-invalid verdict) that FakeQwen's real fixtures
    never produce."""

    def __init__(self, content: str):
        self.usage = UsageMeter()
        self._content = content

    def chat(self, model, messages, *, thinking=False, json_response=True, tools=None):
        return ChatResult(
            content=self._content, task_id="stub-1", model=model,
            prompt_tokens=1, completion_tokens=1, thinking=thinking,
        )

    def embed(self, model, texts):
        return [[0.0] * 8 for _ in texts]

    def tts(self, model, text, instructions):
        return b""


def _request_for(curve: str, max_points: int = 360) -> DiagnoseRequest:
    src = CsvSource(SEEDS_DIR / f"{curve}.csv")
    rows = []
    while (r := src.read()) is not None:
        rows.append(r)
    stride = max(1, -(-len(rows) // max_points))
    picked = rows[::stride]
    curve_points = [
        {"ts": r.ts, "temp_c": r.temp_c, "humidity_pct": r.humidity_pct, "door_open": r.door_open, "power_ok": r.power_ok}
        for r in picked
    ]
    acc: dict[str, tuple[int, float]] = {}
    for r in rows:
        d = day_of(r.ts)
        n, s = acc.get(d, (0, 0.0))
        acc[d] = (n + 1, s + r.temp_c)
    daily = [[d, s / n] for d, (n, s) in sorted(acc.items())]
    return DiagnoseRequest(fridge_meta={"fridge_id": "test"}, curve=curve_points, daily_means=daily, trigger={})


# --------------------------------------------------------------------------- classifier

def test_classify_door_ajar():
    f = {"door_open_any": True, "humidity_delta": 30.0, "peak_delta_c": 3.4, "spike_episodes": 1}
    assert classify_features(f) == "door_ajar"


def test_classify_defrost_cycle():
    f = {"door_open_any": False, "humidity_delta": 1.0, "peak_delta_c": 3.2, "spike_episodes": 4}
    assert classify_features(f) == "defrost_cycle"


def test_classify_power_loss_from_gap():
    f = {"gap_max_s": 2400, "gap_ratio": 240.0}
    assert classify_features(f) == "power_loss"


def test_classify_power_loss_from_mains_flag():
    assert classify_features({"power_out_seen": True}) == "power_loss"


def test_classify_compressor_from_drift():
    f = {"drift_days": 5, "drift_c_per_day": 0.4, "spike_episodes": 4, "humidity_delta": 1.0}
    assert classify_features(f) == "compressor_degradation"


def test_classify_unknown_when_flat():
    assert classify_features({"peak_delta_c": 0.2}) == "unknown"


def test_classifier_precedence_power_over_drift():
    # power evidence wins even if drift is present
    f = {"power_out_seen": True, "drift_days": 5, "drift_c_per_day": 0.5}
    assert classify_features(f) == "power_loss"


def test_classify_ambient_heat_when_only_peak_is_elevated():
    # no door/humidity co-signal, no periodicity, no drift, no gap — just sustained heat.
    assert classify_features({"peak_delta_c": 2.0}) == "ambient_heat"


# --------------------------------------------------------------------------- fixture verdicts

@pytest.mark.parametrize("cause", ["door_ajar", "defrost_cycle", "compressor_degradation", "power_loss", "ambient_heat", "unknown"])
def test_fixture_verdicts_are_schema_valid(cause):
    v = fixture_verdict(cause, {"peak_delta_c": 3.2, "max_rise_c_per_min": 0.8, "humidity_delta": 30.0, "drift_c_per_day": 0.4, "drift_days": 5, "gap_max_s": 2400})
    # the pipeline backfills a citation for CRITICAL verdicts; emulate it
    from permafrost.verdict import is_critical_dict
    if is_critical_dict(v) and not v["guidance_citation"]:
        v["guidance_citation"] = "CDC-style guidance"
    assert ExcursionVerdict.model_validate(v).cause == cause


def test_door_fixture_is_critical_defrost_is_benign():
    door = fixture_verdict("door_ajar", {"max_rise_c_per_min": 0.8, "humidity_delta": 30.0})
    from permafrost.verdict import is_critical_dict
    assert is_critical_dict(door) and not door["benign"]
    defrost = fixture_verdict("defrost_cycle", {"peak_delta_c": 3.2})
    assert defrost["benign"] and not is_critical_dict(defrost)


# --------------------------------------------------------------------------- embeddings

def test_hash_embed_deterministic_and_unit_norm():
    a = hash_embed("door ajar gasket seal")
    b = hash_embed("door ajar gasket seal")
    assert a == b and len(a) == 256
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9


def test_hash_embed_of_empty_text_falls_back_to_a_unit_vector():
    # no tokens -> the all-zero vector has no norm to divide by
    v = hash_embed("")
    assert v[0] == 1.0 and all(x == 0.0 for x in v[1:])
    assert sum(x * x for x in v) == 1.0


def test_hash_embed_similar_text_more_similar():
    def cos(u, v):
        return sum(x * y for x, y in zip(u, v))
    q = hash_embed("power failure outage protocol")
    near = hash_embed("power failure outage keep doors closed")
    far = hash_embed("automatic defrost cycle benign periodic spike")
    assert cos(q, near) > cos(q, far)


# --------------------------------------------------------------------------- guidance store

def test_guidance_retrieve_top_k_deterministic():
    store = GuidanceStore(FakeQwen())
    a = store.retrieve(store.query_for("door_ajar", {"peak_delta_c": 3.0}), k=2)
    b = store.retrieve(store.query_for("door_ajar", {"peak_delta_c": 3.0}), k=2)
    assert [s.id for s, _ in a] == [s.id for s, _ in b]
    assert len(a) == 2


def test_format_citation_truncates_long_text():
    s = Snippet(id="x", source="SRC", title="T", text="y" * 300)
    cit = format_citation(s)
    assert cit.startswith("SRC — T:") and cit.endswith("...") and len(cit) < 200


def test_cosine_similarity_of_a_zero_vector_is_zero():
    assert _cosine([0.0, 0.0], [1.0, 2.0]) == 0.0
    assert _cosine([1.0, 2.0], [0.0, 0.0]) == 0.0


# --------------------------------------------------------------------------- full pipeline

@pytest.mark.parametrize("curve,expected", [
    ("door_ajar", "door_ajar"),
    ("defrost_cycle", "defrost_cycle"),
    ("power_loss", "power_loss"),
    ("compressor_drift", "compressor_degradation"),
])
def test_diagnose_classifies_each_seed_curve(curve, expected):
    transport = FakeQwen()
    store = GuidanceStore(transport)
    result = diagnose(_request_for(curve), transport, store)
    assert result.verdict["cause"] == expected
    assert result.model == MODEL_DIAGNOSIS and result.thinking is True
    assert result.task_id.startswith("fake-")


def test_diagnose_is_deterministic():
    r1 = diagnose(_request_for("door_ajar"), FakeQwen(), GuidanceStore(FakeQwen()))
    r2 = diagnose(_request_for("door_ajar"), FakeQwen(), GuidanceStore(FakeQwen()))
    assert r1.verdict == r2.verdict and r1.task_id == r2.task_id


def test_diagnose_critical_carries_citation_I4():
    result = diagnose(_request_for("door_ajar"), FakeQwen(), GuidanceStore(FakeQwen()))
    assert result.verdict["guidance_citation"].strip()  # backfilled if the model left it empty
    assert result.guidance_ids  # retrieval actually returned snippets


def test_diagnose_request_rejects_single_point_curve():
    with pytest.raises(Exception):
        DiagnoseRequest(curve=[{"ts": 0, "temp_c": 4}])


def test_diagnose_request_forbids_extra_fields():
    with pytest.raises(Exception):
        DiagnoseRequest(curve=[{"ts": 0}, {"ts": 1}], surprise=1)


def test_diagnose_verdict_revalidates_as_excursion_verdict():
    result = diagnose(_request_for("power_loss"), FakeQwen(), GuidanceStore(FakeQwen()))
    ExcursionVerdict.model_validate(result.verdict)  # must not raise


def test_transport_rejects_unlisted_model():
    with pytest.raises(ValueError):
        FakeQwen().chat("gpt-4o", [{"role": "user", "content": "hi"}])
    assert MODEL_DIAGNOSIS in ALLOWED_MODELS


def test_diagnose_raises_on_non_json_model_output():
    transport = _StubTransport("this is not json")
    with pytest.raises(ValueError, match="non-JSON"):
        diagnose(_request_for("door_ajar"), transport, GuidanceStore(transport))


def test_diagnose_raises_when_verdict_fails_schema_validation():
    incomplete = json.dumps({"cause": "door_ajar"})  # missing confidence/benign/evidence/...
    transport = _StubTransport(incomplete)
    with pytest.raises(ValueError, match="schema validation"):
        diagnose(_request_for("door_ajar"), transport, GuidanceStore(transport))


# --------------------------------------------------------------------------- FakeQwen's own fallbacks

def test_fake_qwen_chat_without_a_payload_block_falls_back_to_echo():
    transport = FakeQwen()
    result = transport.chat(MODEL_DIAGNOSIS, [{"role": "user", "content": "no machine-readable block here"}])
    body = json.loads(result.content)
    assert body["note"] == "fake-qwen: unrecognized task"
    assert body["echo"] == {}


def test_fake_qwen_chat_with_malformed_payload_json_falls_back_to_echo():
    transport = FakeQwen()
    messages = [{"role": "user", "content": "```json\nnot valid json {{\n```"}]
    result = transport.chat(MODEL_DIAGNOSIS, messages)
    body = json.loads(result.content)
    assert body["note"] == "fake-qwen: unrecognized task"


def test_fake_qwen_embed_rejects_unlisted_model():
    with pytest.raises(ValueError):
        FakeQwen().embed("gpt-4o", ["hello"])


def test_fake_qwen_tts_returns_deterministic_bytes():
    transport = FakeQwen()
    a = transport.tts(MODEL_TTS, "door open", "urgent but calm")
    b = transport.tts(MODEL_TTS, "door open", "urgent but calm")
    assert a == b and a.startswith(b"FAKE-TTS-PCM:")
    assert transport.usage.totals()[MODEL_TTS]["calls"] == 2


def test_fake_qwen_tts_rejects_unlisted_model():
    with pytest.raises(ValueError):
        FakeQwen().tts("gpt-4o", "hello", "calmly")
