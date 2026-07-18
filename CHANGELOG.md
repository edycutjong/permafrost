# CHANGELOG


## v1.1.0 (2026-07-18)

### Bug Fixes

- **assets**: Og-image chip LIVE→BUILT ON QWEN CLOUD (honest pre-deploy)
  ([`3780aff`](https://github.com/edycutjong/permafrost/commit/3780aff3633543617c51739ab30077b8382ac09a))

- **cli**: Render LIVE DashScope transport failures as a clean card (exit 3)
  ([`4096f00`](https://github.com/edycutjong/permafrost/commit/4096f00a22e7e1dc542c8568f3bf2f753cbc7ab3))

- A7 demo beat: a bogus DASHSCOPE_API_KEY no longer dumps a rich traceback; the CLI renders a
  judge-facing card preserving the real request_id proof (auth failure, non-401 status, and
  connection errors), exit code 3. - 5 new tests mock the transport errors (no network in tests):
  331 total. - Correct GitHub handle edycu -> edycutjong in README badge, pyproject Homepage, and
  the git remote. - Update test-count claims (326 -> 331) in README, DEMO.md, infra/fc/PROOF.md;
  refresh A7 / live-mode expected output in DEMO.md and README.

- **version**: Fastapi app reports package __version__ instead of hardcoded 0.1.0
  ([`2472e35`](https://github.com/edycutjong/permafrost/commit/2472e35860ff43f59d81bbae8626ff8cdeadb780))

### Continuous Integration

- Add Stage 6 semantic-release to pipeline + versioning docs
  ([`c1a10e2`](https://github.com/edycutjong/permafrost/commit/c1a10e26652c73f7a67188e11f5648d007fdcbd7))

### Documentation

- Fix stale 326+ test count in CONTRIBUTING (actual: 331)
  ([`668b2bc`](https://github.com/edycutjong/permafrost/commit/668b2bc896fae114bf4637406435c08176d1cf1d))

### Features

- **site**: Glacial landing + pitch deck + Pages deploy
  ([`15b3c17`](https://github.com/edycutjong/permafrost/commit/15b3c1751da4fb159a5ff2f5d2f34fe911bae739))


## v1.0.0 (2026-07-14)

### Features

- Initial import of permafrost-edge
  ([`c5f6ad0`](https://github.com/edycutjong/permafrost/commit/c5f6ad0181aa5ab6bedadc33848fcb0a1ca594bd))
