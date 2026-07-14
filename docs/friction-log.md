# Permafrost — build friction log

Honest notes from building Permafrost against Qwen Cloud + the edge stack, kept as
developer-experience feedback. "Offline-first" below means the FakeQwen transport +
in-process ASGI link that lets the whole project run keyless and deterministic.

## What went well

- **OpenAI-compatible endpoint** (`https://dashscope-intl.aliyuncs.com/compatible-mode/v1`)
  meant the entire cloud layer is the stock `openai` SDK — one `LiveQwen` class, no
  bespoke client. Swapping `FakeQwen` ⇄ `LiveQwen` behind a `QwenTransport` Protocol was
  a five-line change, so tests/replay never need a key.
- **Structured output** (`response_format={"type":"json_object"}`) made the
  `ExcursionVerdict` contract enforceable: the pydantic model is the safety gate, and a
  malformed verdict fails at parse time, never at the siren.
- **Thinking flag** via `extra_body={"enable_thinking": true}` on `qwen3.7-plus` is the
  right tool for numeric time-series reasoning (slope vs periodicity vs humidity) — a
  flash-tier model pattern-matches "spike = alarm", which is the failure we exist to kill.

## Friction / gotchas (with the workaround we shipped)

1. **Model-id drift is a real risk.** We locked the four ids we actually send into a
   frozen `ALLOWED_MODELS` set (`qwen3.7-plus`, `qwen3.6-flash`, `text-embedding-v4`,
   `qwen3-tts-instruct-flash`) and both transports raise on anything else, so a typo
   can never reach the wire or the bill. Test: `test_transport_rejects_unlisted_model`.
2. **Batch API is async-polling, not webhooks.** For a weekly compliance sweep that is
   fine, but it means the report path can't be a synchronous request in production. We
   render synchronously today and document the Batch pricing as a production plan rather
   than claiming an exercised call (see README Status).
3. **International-endpoint RTT makes a local reflex tier mandatory.** Any cloud round
   trip is far too slow for a door alarm, so the edge must decide in <100 ms from local
   rules and only escalate ambiguity. This is the track's whole point, but it's worth
   stating: the cloud is the *diagnostician*, not the *reflex*.
4. **TTS on the compatible audio surface is under-exercised.** `qwen3-tts-instruct-flash`
   is wired through `LiveQwen.tts`, but we have not made a live audio call in this build;
   the fixture returns deterministic pseudo-PCM. Flagged plainly, not hidden.
5. **Determinism needed engineering, not luck.** To let `bench.py` assert a ≥0.9 accuracy
   floor and the tests assert byte-identical chains, every non-model input had to be
   deterministic: a virtual clock (`VIRTUAL_EPOCH + csv_offset`), canonical JSON for all
   hashing/signing, rounded curve features, and a hashing-trick embedding for offline
   retrieval. A single `time.time()` leak would have made the chain non-reproducible.
6. **SQLite WAL + a resumable CSV source** were what made the power-cut invariant (I1)
   testable: committing every tick means an `os._exit` mid-replay loses nothing, and
   `skip_until_ts` lets a fresh process resume exactly where the killed one stopped. The
   one wrinkle: the resumed daemon re-emits a `link_state` marker on restart, so the
   resumed chain is gap-free but one entry longer than an uninterrupted run — expected
   and asserted, not papered over.
7. **pynacl SealedBox is anonymous-sender by design**, so each seal is randomized
   (ephemeral key) and two seals of the same payload differ — good for privacy, but the
   test had to assert *decrypts-equal* rather than *bytes-equal*.

## If we had another day

- Live `qwen3-tts-instruct-flash` call + a captured audio sample in the video.
- Deploy the FastAPI app to Function Compute and capture the console recording
  (checklist already written in `infra/fc/PROOF.md`).
- `qwen3-rerank` after `text-embedding-v4` to sharpen guidance retrieval on edge cases.
