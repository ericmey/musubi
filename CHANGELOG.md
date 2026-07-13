# Changelog

## [1.0.0](https://github.com/ericmey/musubi/compare/v1.11.7...v1.0.0) (2026-07-13)


### Features

* **_operator:** merge-flow.py — automate per-slice post-merge ritual ([#100](https://github.com/ericmey/musubi/issues/100)) ([be94ad7](https://github.com/ericmey/musubi/commit/be94ad7a26aa31f3f8561f6e7fca14b8e45853aa))
* **_operator:** musubi slice status board ([#105](https://github.com/ericmey/musubi/issues/105)) ([478b181](https://github.com/ericmey/musubi/commit/478b181a78796537c32153feeff880b2e3c991b6))
* **_operator:** render-prompt ([#122](https://github.com/ericmey/musubi/issues/122)) ([fb2a6b0](https://github.com/ericmey/musubi/commit/fb2a6b00652c40a44c8f46765e4b9512f716ec8c))
* **adapter-livekit:** slice-adapter-livekit ([#96](https://github.com/ericmey/musubi/issues/96)) ([2dfcac7](https://github.com/ericmey/musubi/commit/2dfcac755223630b2eca2d90f173ff9a029a505b))
* **adapters/mcp:** slice-mcp-canonical-tools — canonical 5-tool surface ([#292](https://github.com/ericmey/musubi/issues/292)) ([e1dec86](https://github.com/ericmey/musubi/commit/e1dec867ddfb6712db02d1eedd66cc2cd9b4f9f7))
* **adapters:** slice-adapter-mcp ([#95](https://github.com/ericmey/musubi/issues/95)) ([00a7b67](https://github.com/ericmey/musubi/commit/00a7b6715eee94ce4774cc2dddedef4b23d1a07a))
* **api,sdk:** operator-gated created_at override on capture (closes [#140](https://github.com/ericmey/musubi/issues/140)) ([#203](https://github.com/ericmey/musubi/issues/203)) ([a668560](https://github.com/ericmey/musubi/commit/a668560e2b09b8485f8079ba1f6b3d2d94f1ae7d))
* **api:** debug-synthesis-trigger endpoint + [#119](https://github.com/ericmey/musubi/issues/119) ollama-offline unskip ([#139](https://github.com/ericmey/musubi/issues/139)) ([9897a1e](https://github.com/ericmey/musubi/commit/9897a1ee8171f2f2358bb99b036af042f79f03fb))
* **api:** expose state_filter on POST /v1/retrieve body ([#271](https://github.com/ericmey/musubi/issues/271)) ([e34fffe](https://github.com/ericmey/musubi/commit/e34fffe4f6bffafd575d52d954a8d995c08171e1))
* **api:** implement Last-Event-ID replay on /v1/thoughts/stream ([#238](https://github.com/ericmey/musubi/issues/238)) ([f718116](https://github.com/ericmey/musubi/commit/f718116c18b70223af5408f10884d29ffc0af634))
* **api:** promote `title` to top-level on RetrieveResultRow (breaking) ([#236](https://github.com/ericmey/musubi/issues/236)) ([5e79151](https://github.com/ericmey/musubi/commit/5e79151f176879e452f112fa78762aa85bc26b5b))
* **api:** rename endpoints to plane-aligned paths (breaking, v1.0) ([#237](https://github.com/ericmey/musubi/issues/237)) ([ea16b3d](https://github.com/ericmey/musubi/commit/ea16b3d477feee32e4f9bc61f11d0306a9a050d7))
* **api:** slice-api-app-bootstrap ([#126](https://github.com/ericmey/musubi/issues/126)) ([2eec59a](https://github.com/ericmey/musubi/commit/2eec59a2a62c03f68d49219dbc0afc60f5fbb15e))
* **api:** slice-api-thoughts-stream ([#106](https://github.com/ericmey/musubi/issues/106)) ([c1c8dd6](https://github.com/ericmey/musubi/commit/c1c8dd6a0856a4550f48cbacf76fcb58e05b6b33))
* **api:** slice-api-v0-read ([#73](https://github.com/ericmey/musubi/issues/73)) ([b66bfa9](https://github.com/ericmey/musubi/commit/b66bfa9c4462769abdb89f6ad11c1df2794cb92f))
* **api:** slice-api-v0-write ([#78](https://github.com/ericmey/musubi/issues/78)) ([71c2acb](https://github.com/ericmey/musubi/commit/71c2acbb55e78a32fe24f8b040f7f2ba289f265d))
* **api:** wildcard namespace segments for tenant-wide retrieve ([#268](https://github.com/ericmey/musubi/issues/268)) ([e9ef0be](https://github.com/ericmey/musubi/commit/e9ef0befb576ec49ddbc299da4ba5ba755df86f4))
* **auth:** slice-auth ([#38](https://github.com/ericmey/musubi/issues/38)) ([6f1eb4c](https://github.com/ericmey/musubi/commit/6f1eb4c437e49e635baf50922508043c6e216908))
* **ci:** auto-enable auto-merge on auto-digest-bump PRs ([#252](https://github.com/ericmey/musubi/issues/252)) ([e44abd0](https://github.com/ericmey/musubi/commit/e44abd076846fe14b87415a2b9ab090b328abb10))
* **ci:** auto-enable auto-merge on release-please PRs ([#245](https://github.com/ericmey/musubi/issues/245)) ([774f5e7](https://github.com/ericmey/musubi/commit/774f5e7f8682cef71c2e5dbe5717450dd62495bb))
* **cli:** operator CLI for promote force / reject ([#227](https://github.com/ericmey/musubi/issues/227)) ([b928739](https://github.com/ericmey/musubi/commit/b928739edb9785dc6599a0bccc6dbc0c8a4f0247))
* **config:** settings loader for slice-config ([0d6ff96](https://github.com/ericmey/musubi/commit/0d6ff963070da65519bc37b7bef8ae5e29b47bb6))
* **embedding:** batching + caching + truncation (slice-embedding follow-up) ([#41](https://github.com/ericmey/musubi/issues/41)) ([907fb17](https://github.com/ericmey/musubi/commit/907fb175a151b22c929899505887fa91345c1321))
* **embedding:** Embedder protocol, TEI clients, and FakeEmbedder for slice-embedding ([0f59ab9](https://github.com/ericmey/musubi/commit/0f59ab915f740939be7c482e265ac45707eab80e))
* **ingestion:** slice-ingestion-capture ([#86](https://github.com/ericmey/musubi/issues/86)) ([32e97dd](https://github.com/ericmey/musubi/commit/32e97ddbdcb3e821c8c8c723bc2edd77a66a6fa7))
* **integration:** slice-ops-integration-harness ([#114](https://github.com/ericmey/musubi/issues/114)) ([68313e2](https://github.com/ericmey/musubi/commit/68313e2d709081896e09f476d6173762305b62e5))
* **lifecycle:** artifact archival sweep (opt-in) + slice-ops-storage ([#226](https://github.com/ericmey/musubi/issues/226)) ([50f0c59](https://github.com/ericmey/musubi/commit/50f0c5947da683e19372f99068be35d3bab19f39))
* **lifecycle:** per-family synthesis with candidates pool ([#335](https://github.com/ericmey/musubi/issues/335)) ([e2b8377](https://github.com/ericmey/musubi/commit/e2b83775dc2af9d47d03e9137d064ca7f5f708b4))
* **lifecycle:** promotion-builder ([#171](https://github.com/ericmey/musubi/issues/171)) ([7913db5](https://github.com/ericmey/musubi/commit/7913db55fdd55215158a7074f9807e1f9e35c934))
* **lifecycle:** reflection-builder closes P3 ([#172](https://github.com/ericmey/musubi/issues/172)) ([227c658](https://github.com/ericmey/musubi/commit/227c6587c08f471dc1f69394157625721975a8df))
* **lifecycle:** slice-lifecycle-engine ([#40](https://github.com/ericmey/musubi/issues/40)) ([89e78d9](https://github.com/ericmey/musubi/commit/89e78d98563e1f67c730a6b89447c14599c71099))
* **lifecycle:** slice-lifecycle-maturation ([#52](https://github.com/ericmey/musubi/issues/52)) ([942d048](https://github.com/ericmey/musubi/commit/942d048d868bd06fb3bd797314f6134b45add8a1))
* **lifecycle:** slice-lifecycle-promotion ([#68](https://github.com/ericmey/musubi/issues/68)) ([80e1fb8](https://github.com/ericmey/musubi/commit/80e1fb8a437f9cf7ce9acbdb45ee03d1901e7350))
* **lifecycle:** slice-lifecycle-reflection ([#57](https://github.com/ericmey/musubi/issues/57)) ([afb75fb](https://github.com/ericmey/musubi/commit/afb75fb277d7f1cb443a592663e5579926b8b442))
* **lifecycle:** slice-lifecycle-synthesis ([#62](https://github.com/ericmey/musubi/issues/62)) ([373374b](https://github.com/ericmey/musubi/commit/373374bb6cbdc06609ae3534c1175e2f50b946b8))
* **lifecycle:** synthesis-builder ([#165](https://github.com/ericmey/musubi/issues/165)) ([7770a03](https://github.com/ericmey/musubi/commit/7770a03fdf64979fc87bd8a4eb5bb16e11a8260c))
* **lifecycle:** wire OTel tracing in lifecycle-worker ([#317](https://github.com/ericmey/musubi/issues/317)) ([ff75a68](https://github.com/ericmey/musubi/commit/ff75a68659355cfd6060fe45f93c0a650d69367a))
* **lifecycle:** wire real demotion jobs into the runner ([#163](https://github.com/ericmey/musubi/issues/163)) ([3c0bfc6](https://github.com/ericmey/musubi/commit/3c0bfc6a747389e489cae47d58e06d499a12b638))
* **migration:** slice-poc-data-migration ([#128](https://github.com/ericmey/musubi/issues/128)) ([4948ff5](https://github.com/ericmey/musubi/commit/4948ff5e5516c240e8e406e45ddc9f2bccda676f))
* **observability:** lifecycle-worker /metrics exposition ([#346](https://github.com/ericmey/musubi/issues/346)) ([de71ba7](https://github.com/ericmey/musubi/commit/de71ba70f719aa51d4e8cc1356f36d1bbecfd6c2))
* **observability:** scrape qdrant metrics via Bearer-authed Prometheus job ([#314](https://github.com/ericmey/musubi/issues/314)) ([9d1ddeb](https://github.com/ericmey/musubi/commit/9d1ddeb49b5e801383b43379cc911639e3f4da8b))
* **observability:** slice-ops-observability ([#104](https://github.com/ericmey/musubi/issues/104)) ([39e422e](https://github.com/ericmey/musubi/commit/39e422e74da7cde2e166ca5d67792278d1870e39))
* **ops:** brain + backup + Prometheus + GHCR publish + update.yml ([#154](https://github.com/ericmey/musubi/issues/154)) ([c0c479a](https://github.com/ericmey/musubi/commit/c0c479a1dd12da39fa2c2579b569c974d63831e7))
* **ops:** musubi-core Dockerfile + compose/env template fixes for real deploy ([#148](https://github.com/ericmey/musubi/issues/148)) ([318e509](https://github.com/ericmey/musubi/commit/318e509b2b61387ea58d3efe16c1e30b02ce1840))
* **ops:** perf-testing harness + manual-recovery runbook (Gate 0) ([#191](https://github.com/ericmey/musubi/issues/191)) ([0976622](https://github.com/ericmey/musubi/commit/0976622f8b30890185015bcc39841f9c893c6c96))
* **ops:** slice-ops-ansible ([#77](https://github.com/ericmey/musubi/issues/77)) ([a7ffca0](https://github.com/ericmey/musubi/commit/a7ffca091afa5a949de5514b1e8baa9761ab6f12))
* **ops:** slice-ops-backup ([#82](https://github.com/ericmey/musubi/issues/82)) ([a26a504](https://github.com/ericmey/musubi/commit/a26a50425c791ac79f0bf99bde5c9831dcab82e6))
* **ops:** slice-ops-compose ([#85](https://github.com/ericmey/musubi/issues/85)) ([d59435a](https://github.com/ericmey/musubi/commit/d59435ae08eb51d60b20a601acbaf2abf08bea4a))
* **ops:** slice-ops-first-deploy ([#121](https://github.com/ericmey/musubi/issues/121)) ([3bf543e](https://github.com/ericmey/musubi/commit/3bf543eefd5cdd4e86c17d6a81ec6b841bd4c635))
* **ops:** slice-ops-hardening-suite ([#125](https://github.com/ericmey/musubi/issues/125)) ([363c515](https://github.com/ericmey/musubi/commit/363c5154857c0a0425a554dd3afb2f0d87b34cc9))
* **plane:** batch_create + longer-wins merge strategy (closes [#141](https://github.com/ericmey/musubi/issues/141), [#142](https://github.com/ericmey/musubi/issues/142)) ([#204](https://github.com/ericmey/musubi/issues/204)) ([eeaae56](https://github.com/ericmey/musubi/commit/eeaae5681e323c302c6846b12c5713e759957ffb))
* **planes:** first cut of the episodic plane for slice-plane-episodic ([ed48aa4](https://github.com/ericmey/musubi/commit/ed48aa48b63ce3cd9cacdd0bd643524255c5ea2d))
* **planes:** slice-plane-artifact ([#51](https://github.com/ericmey/musubi/issues/51)) ([25e7ac9](https://github.com/ericmey/musubi/commit/25e7ac9639f984dab68132b2f5c1e419a182103d))
* **planes:** slice-plane-concept ([#42](https://github.com/ericmey/musubi/issues/42)) ([2f1cc7e](https://github.com/ericmey/musubi/commit/2f1cc7e7072fae87a42f51a076ffbcb63232f22c))
* **planes:** slice-plane-curated ([#39](https://github.com/ericmey/musubi/issues/39)) ([051d894](https://github.com/ericmey/musubi/commit/051d894e821155cd1f532dfa0ce105aae19ff38f))
* **planes:** slice-plane-episodic-followup ([#94](https://github.com/ericmey/musubi/issues/94)) ([d78bec9](https://github.com/ericmey/musubi/commit/d78bec9a8bbfc99f6bb11b808f09276cbff17a44))
* **planes:** slice-plane-thoughts ([#49](https://github.com/ericmey/musubi/issues/49)) ([2e62a39](https://github.com/ericmey/musubi/commit/2e62a391f032ca9fe06170d051ddd717dfb7ccdc))
* release v1.0 — API sealed, integrations on canonical, automation hands-off ([#264](https://github.com/ericmey/musubi/issues/264)) ([92fab8b](https://github.com/ericmey/musubi/commit/92fab8bc202f5b273638f111ac53a5d284345ae5))
* **retrieve:** 2-segment namespace + cross-plane fanout, strict scope (closes [#209](https://github.com/ericmey/musubi/issues/209)) ([#210](https://github.com/ericmey/musubi/issues/210)) ([1d9c2e1](https://github.com/ericmey/musubi/commit/1d9c2e1f17330e7e91cd3180aeea44e1ae483224))
* **retrieve:** add context-pack endpoint ([#379](https://github.com/ericmey/musubi/issues/379)) ([6bb0591](https://github.com/ericmey/musubi/commit/6bb059112ff9fb6e0c73670d4213465d30aed819))
* **retrieve:** federate hybrid retrieval by identity_family ([#334](https://github.com/ericmey/musubi/issues/334)) ([61c99ae](https://github.com/ericmey/musubi/commit/61c99aebda3d5203ed11127fee93877bf1815dc5))
* **retrieve:** mode=recent (slice-retrieve-recent) ([#343](https://github.com/ericmey/musubi/issues/343)) ([6695f5d](https://github.com/ericmey/musubi/commit/6695f5d86c40335cd699ad6bbab10baddbc63501))
* **retrieve:** slice-retrieval-blended ([#79](https://github.com/ericmey/musubi/issues/79)) ([3c7ae5b](https://github.com/ericmey/musubi/commit/3c7ae5b42646c7a20f832ecb0215af108cec41fa))
* **retrieve:** slice-retrieval-deep ([#67](https://github.com/ericmey/musubi/issues/67)) ([375958c](https://github.com/ericmey/musubi/commit/375958c14838ed2774579110581f623dd87e6ac3))
* **retrieve:** slice-retrieval-fast ([#74](https://github.com/ericmey/musubi/issues/74)) ([a0687f4](https://github.com/ericmey/musubi/commit/a0687f46acc43cb52ce091b10e2a259409f3449f))
* **retrieve:** slice-retrieval-hybrid ([#50](https://github.com/ericmey/musubi/issues/50)) ([759c050](https://github.com/ericmey/musubi/commit/759c05044999ba5b05a6c6269e128ee10bc00703))
* **retrieve:** slice-retrieval-orchestration ([#87](https://github.com/ericmey/musubi/issues/87)) ([fe75e54](https://github.com/ericmey/musubi/commit/fe75e54ef37e543ece75a6b63ebf5acd1b6bd898))
* **retrieve:** slice-retrieval-rerank ([#60](https://github.com/ericmey/musubi/issues/60)) ([de6dfc9](https://github.com/ericmey/musubi/commit/de6dfc94fb57675b832bd7979ac3d9ea111b4998))
* **retrieve:** slice-retrieval-scoring ([#54](https://github.com/ericmey/musubi/issues/54)) ([f857d5b](https://github.com/ericmey/musubi/commit/f857d5bfaa82586deba42824daceb5faaf315e01))
* **sdk:** slice-sdk-py ([#90](https://github.com/ericmey/musubi/issues/90)) ([89e8f14](https://github.com/ericmey/musubi/commit/89e8f14261a314493d26dc41c8fbde5d2cdbf53d))
* **store:** slice-qdrant-layout first cut — bootstrap + indexes ([b7b3ae9](https://github.com/ericmey/musubi/commit/b7b3ae91cc99ccf90f81849196ad0427d34ca656))
* **tools:** tc_coverage — mechanical Test Contract closure audit; skills-README mirror pattern ([4dec434](https://github.com/ericmey/musubi/commit/4dec4349e4fa51d1ee098bab7ea02a2238c73119))
* **types:** add identity_family field for cross-substrate federation ([#332](https://github.com/ericmey/musubi/issues/332)) ([7d032a9](https://github.com/ericmey/musubi/commit/7d032a99ce3da7c642d761f0452ac5de4d93af20))
* **types:** slice-types first cut — pydantic foundation ([b5849cf](https://github.com/ericmey/musubi/commit/b5849cf76e44dc31a191747c074d8ff38fd11d52))
* **types:** slice-types-followup ([#113](https://github.com/ericmey/musubi/issues/113)) ([856e2c5](https://github.com/ericmey/musubi/commit/856e2c53144fc89d25a4fa1a0d43a0babc07b717))
* **vault:** event rate limit + indexing concurrency cap ([#229](https://github.com/ericmey/musubi/issues/229)) ([6950422](https://github.com/ericmey/musubi/commit/6950422c89715360cad437db16dbde0be74fb9d5))
* **vault:** skip-with-warning for binary + oversize files in sync layer ([#228](https://github.com/ericmey/musubi/issues/228)) ([4cf436d](https://github.com/ericmey/musubi/commit/4cf436d4fa7e2180827f4b3b14b0666b3f4a4816))
* **vault:** slice-vault-sync ([#64](https://github.com/ericmey/musubi/issues/64)) ([eef5e5c](https://github.com/ericmey/musubi/commit/eef5e5cb328a68c1bf9420f9742bb87e3a28f0a2))
* **vault:** wire vault_reconcile into lifecycle scheduler (partial musubi[#345](https://github.com/ericmey/musubi/issues/345)) ([#357](https://github.com/ericmey/musubi/issues/357)) ([eb49125](https://github.com/ericmey/musubi/commit/eb4912557807ddf332f8453e522fb5459ca3fdf8))


### Bug Fixes

* **_operator:** accept chore(slice): handoff as valid prefix ([#99](https://github.com/ericmey/musubi/issues/99)) ([5b15cf4](https://github.com/ericmey/musubi/commit/5b15cf4866c522f02c6520b28bde47035d2d51cf))
* **_operator:** guard merge-flow.py flip + reconcile observability paths ([54d5bf5](https://github.com/ericmey/musubi/commit/54d5bf5d3811f5e3ba9b96aec978262f6b75aa26))
* **_operator:** merge-flow.py orphan-sweep survives auto-deleted branches ([#115](https://github.com/ericmey/musubi/issues/115)) ([4589190](https://github.com/ericmey/musubi/commit/45891903ce058b4af7c3159eab40971444a7a445))
* **_tools:** ast-based skip-reason scan for tc-coverage ([#91](https://github.com/ericmey/musubi/issues/91)) ([a7627b5](https://github.com/ericmey/musubi/commit/a7627b5339b901ad88a5bf6b7abb1b1d458c7dcb))
* **_tools:** downgrade done slice overlap to warning ([#101](https://github.com/ericmey/musubi/issues/101)) ([a814ccb](https://github.com/ericmey/musubi/commit/a814ccb02e980cbac3c9591a6c23b5cc720ca31b))
* **_tools:** promote frontmatter/label drift to error for review/done states ([#93](https://github.com/ericmey/musubi/issues/93)) ([13b004f](https://github.com/ericmey/musubi/commit/13b004f63ca78229d56f24ff92b0030924e4c1e4))
* **adapters:** add typed episode tags to captures ([#386](https://github.com/ericmey/musubi/issues/386)) ([1663e20](https://github.com/ericmey/musubi/commit/1663e208fee3211018281b4f1cda3123a2ff6327))
* **ansible:** restore inventory.yml indentation ([#178](https://github.com/ericmey/musubi/issues/178)) ([0cdb065](https://github.com/ericmey/musubi/commit/0cdb0652c05a8e2dd131bfbdbc67835b311cca81))
* **api,test:** clarify trigger-synthesis namespace semantics ([#354](https://github.com/ericmey/musubi/issues/354)) ([4cb9739](https://github.com/ericmey/musubi/commit/4cb9739538a688fc61e30ba075851d034f5d33d8))
* **api:** a corrupted memory must be removable, must not be creatable, and retraction must keep working ([#398](https://github.com/ericmey/musubi/issues/398)) ([4f574da](https://github.com/ericmey/musubi/commit/4f574da285f8e3a3854e0d84c09d70212dbda026))
* **api:** default episodic typed tags ([#392](https://github.com/ericmey/musubi/issues/392)) ([09a0d73](https://github.com/ericmey/musubi/commit/09a0d73037a31064c041ae8d7caa0bf7b9d3cb9e))
* **api:** fix 500 on non-trivial artifact upload ([#135](https://github.com/ericmey/musubi/issues/135)) ([40d10b6](https://github.com/ericmey/musubi/commit/40d10b6f23e296b97092ec88da171ac0c6b67528))
* **api:** map pydantic ValidationError to 422 (closes [#192](https://github.com/ericmey/musubi/issues/192)) ([#201](https://github.com/ericmey/musubi/issues/201)) ([6196283](https://github.com/ericmey/musubi/commit/6196283be49ac7b39f9ed8d6d325a62f73fbfccb))
* **api:** ops/status probes ollama at /api/tags, not /health ([#153](https://github.com/ericmey/musubi/issues/153)) ([6039f79](https://github.com/ericmey/musubi/commit/6039f7930332fddecea83b2eaf18985970dd15e0))
* **api:** wire POST /v1/retrieve to the real orchestration pipeline ([#197](https://github.com/ericmey/musubi/issues/197)) ([9f80644](https://github.com/ericmey/musubi/commit/9f8064491aba851f2850d846a2b530b2ddfdece3))
* **artifact:** tokenizer wiring for chunkers (rescue finish for Codex) ([#132](https://github.com/ericmey/musubi/issues/132)) ([d6ab78f](https://github.com/ericmey/musubi/commit/d6ab78ff8c458e86bcd7464104f25862ffe82efa))
* **auth:** make recursive scope read-only ([#387](https://github.com/ericmey/musubi/issues/387)) ([3fd8dbd](https://github.com/ericmey/musubi/commit/3fd8dbd175294c5419b2d447b69e524675b7daf1))
* **ci:** auto-digest-bump trigger + auth (closes [#247](https://github.com/ericmey/musubi/issues/247)) ([#249](https://github.com/ericmey/musubi/issues/249)) ([8540a3c](https://github.com/ericmey/musubi/commit/8540a3ca7a2bff966841ab848ccd2223d57df025))
* **ci:** grant actions:read so upload-sarif can fingerprint workflow run ([#167](https://github.com/ericmey/musubi/issues/167)) ([bcdc1bb](https://github.com/ericmey/musubi/commit/bcdc1bb896cfdb19376ffa880c92483262e85264))
* **ci:** grant contents:write so release-body append can update release (closes [#242](https://github.com/ericmey/musubi/issues/242)) ([#243](https://github.com/ericmey/musubi/issues/243)) ([b98215c](https://github.com/ericmey/musubi/commit/b98215cb6621eb2afa3862a0a33c38fe25fcd37b))
* **ci:** grant security-events:write + surface Trivy findings in table format ([#162](https://github.com/ericmey/musubi/issues/162)) ([d5fe94f](https://github.com/ericmey/musubi/commit/d5fe94f202e87bdf99dcd30eecf8e0eeb0741dc8))
* **ci:** release-please tag push must use a PAT, not GITHUB_TOKEN ([#215](https://github.com/ericmey/musubi/issues/215)) ([a775581](https://github.com/ericmey/musubi/commit/a775581df12e7bba4534ec8c95d27dce8ade69a4))
* **ci:** scope Trivy SARIF gate to CRITICAL via limit-severities-for-sarif ([#166](https://github.com/ericmey/musubi/issues/166)) ([78ffe0e](https://github.com/ericmey/musubi/commit/78ffe0eb410bd0cea24fbb79580b039a2503ea40))
* **ci:** skip Integration on release-please PRs ([#350](https://github.com/ericmey/musubi/issues/350)) ([8b7db98](https://github.com/ericmey/musubi/commit/8b7db987a0f326cd95ec9e9e0bcd87d87a03b29d))
* **ci:** tolerate Security-tab upload failure on private repo ([#170](https://github.com/ericmey/musubi/issues/170)) ([066fbe1](https://github.com/ericmey/musubi/commit/066fbe18093ca8b315d5aea7930f2cf43b79bc84))
* **ci:** trivy-action version — 0.28.0 doesn't exist; use 0.35.0 ([#161](https://github.com/ericmey/musubi/issues/161)) ([9be1f0d](https://github.com/ericmey/musubi/commit/9be1f0ddee001dfd815765754df89c074ed927a1))
* cold-cache retrieve delay ([#133](https://github.com/ericmey/musubi/issues/133)) ([#138](https://github.com/ericmey/musubi/issues/138)) ([1ca98d2](https://github.com/ericmey/musubi/commit/1ca98d2b5cc0b95a492424d793e0921297c8453b))
* **curated:** same-id new-body is an UPDATE, not supersession (closes [#362](https://github.com/ericmey/musubi/issues/362)) ([#363](https://github.com/ericmey/musubi/issues/363)) ([9ed6f68](https://github.com/ericmey/musubi/commit/9ed6f6848f1fb3f6a123c59a6979716c6cdb294c))
* **deploy:** bake tokenizers into image, fix /app cache permissions ([#327](https://github.com/ericmey/musubi/issues/327)) ([7dd406a](https://github.com/ericmey/musubi/commit/7dd406aa28dc29b154711294ab1667ccb93557e1))
* **deploy:** refresh env during image updates ([#395](https://github.com/ericmey/musubi/issues/395)) ([cb09a51](https://github.com/ericmey/musubi/commit/cb09a51031c1852674f0eeda3822aaf2fc44162b))
* **deploy:** remove stale per-version commentary on the core-image pin ([#260](https://github.com/ericmey/musubi/issues/260)) ([7acd357](https://github.com/ericmey/musubi/commit/7acd35714ecff5959cc005e758447c0540d8beb8))
* **deploy:** target main for image bump and rollback ([#415](https://github.com/ericmey/musubi/issues/415)) ([cb8f4a0](https://github.com/ericmey/musubi/commit/cb8f4a08dd694e3545f5c7048f6a8640475316c7))
* **docs:** path-prefix the ADR-0028 wikilink in ADR 0030 ([#257](https://github.com/ericmey/musubi/issues/257)) ([baaab32](https://github.com/ericmey/musubi/commit/baaab3259da182315aa645c87c6a7176625e8b5f))
* **embedding:** chunk + max-pool sparse inputs over SPLADE's 512-token cap ([#324](https://github.com/ericmey/musubi/issues/324)) ([3c31a23](https://github.com/ericmey/musubi/commit/3c31a23e395045d998a164f371942825b88fb269))
* **embedding:** raise TEI client char-truncate from 2048 to 32000 ([#330](https://github.com/ericmey/musubi/issues/330)) ([0a33d7b](https://github.com/ericmey/musubi/commit/0a33d7b0e5decd410bbf69e9e4a7cca33eab8ca3))
* **embedding:** rebuild TEI httpx.AsyncClient per asyncio loop ([#321](https://github.com/ericmey/musubi/issues/321)) ([7a66a03](https://github.com/ericmey/musubi/commit/7a66a03ec8dc6c2a1ecc7e9bf433cd19036443d5))
* **embedding:** reuse a pooled httpx.AsyncClient per TEI client ([#198](https://github.com/ericmey/musubi/issues/198)) ([5a24e28](https://github.com/ericmey/musubi/commit/5a24e280906db65b26d73e17c6332466e6e72dbc))
* **image:** patch gnutls CRITICAL CVEs + retry HF tokenizer prefetch ([#375](https://github.com/ericmey/musubi/issues/375)) ([f34a7df](https://github.com/ericmey/musubi/commit/f34a7df2558209e9eb99cf8b6de42093b0155484))
* **lifecycle:** concept_maturation_sweep skips concepts with active contradicts ([#65](https://github.com/ericmey/musubi/issues/65)) ([a99e8ea](https://github.com/ericmey/musubi/commit/a99e8eaf1da72086cf4ecf1730d160311f44033b))
* **lifecycle:** emit worker job metrics ([#130](https://github.com/ericmey/musubi/issues/130)) ([84f1a88](https://github.com/ericmey/musubi/commit/84f1a881e81bffc5560884c4f819f214890f015f))
* **lifecycle:** stop registering demotion jobs from build_maturation_jobs ([#181](https://github.com/ericmey/musubi/issues/181)) ([da0f2e9](https://github.com/ericmey/musubi/commit/da0f2e91e9a0be33e629b1df9f92952d711acdb6))
* **lifecycle:** wire promotion_attempts gate + last_reinforced_at reinforce clock ([#225](https://github.com/ericmey/musubi/issues/225)) ([58eea81](https://github.com/ericmey/musubi/commit/58eea812f17fe2e0beca41d892728a6bad93b2bd))
* **lifecycle:** wrap embedder in ChunkedEmbedder (closes [#367](https://github.com/ericmey/musubi/issues/367)) ([#369](https://github.com/ericmey/musubi/issues/369)) ([e7c781d](https://github.com/ericmey/musubi/commit/e7c781de6ad04ac0aa53597c80e6802429891f6f))
* **llm:** constrain Ollama decoding with Pydantic JSON Schema ([#311](https://github.com/ericmey/musubi/issues/311)) ([0909a93](https://github.com/ericmey/musubi/commit/0909a93dd0654b43d6fc4e22cf2f120f78fae47b))
* **mcp:** musubi_get 2-part namespace foot-gun + wire musubi_recent ([#288](https://github.com/ericmey/musubi/issues/288)) ([#373](https://github.com/ericmey/musubi/issues/373)) ([1df9e62](https://github.com/ericmey/musubi/commit/1df9e620907e7654c40d086fe10fdcb4108ef47f))
* **mcp:** route adapter env reads through get_settings (v2 CI hotfix) ([ae9828d](https://github.com/ericmey/musubi/commit/ae9828d55e766022986060ad0de094595228ddcf))
* **observability:** complete server-side OTel tracing (debt from slice-ops-observability) ([#303](https://github.com/ericmey/musubi/issues/303)) ([b04c3b1](https://github.com/ericmey/musubi/commit/b04c3b1ffcc2f7289b156aab5c3192955d0f286d))
* **operator:** detect file-owned paths in handoff audit ([#127](https://github.com/ericmey/musubi/issues/127)) ([f4a6892](https://github.com/ericmey/musubi/commit/f4a6892d00651c5b58e696e800bf14be69c7fa53))
* **ops:** expose service version in status ([#383](https://github.com/ericmey/musubi/issues/383)) ([a081501](https://github.com/ericmey/musubi/commit/a0815013c7335037005194e75da6264077f148f0))
* **ops:** perf harness — idempotent seed + remote telemetry + 429 backoff ([#194](https://github.com/ericmey/musubi/issues/194)) ([85cf5be](https://github.com/ericmey/musubi/commit/85cf5be506ef41d41cff889594f722bf3789c745))
* **ops:** perf-harness round 3 — fixes found during live Gate 1 run ([#196](https://github.com/ericmey/musubi/issues/196)) ([1031104](https://github.com/ericmey/musubi/commit/1031104d0e838d6a02a704597bc752d017448451))
* **ops:** repair backup/restore playbook (closes [#190](https://github.com/ericmey/musubi/issues/190)) ([#202](https://github.com/ericmey/musubi/issues/202)) ([63e0765](https://github.com/ericmey/musubi/commit/63e0765c6df0adc3c9d4ab89a70f475a69b8ee84))
* **rate-limit:** delete dead settings-backed rate fields (closes [#193](https://github.com/ericmey/musubi/issues/193)) ([#206](https://github.com/ericmey/musubi/issues/206)) ([b8e98d5](https://github.com/ericmey/musubi/commit/b8e98d53c4e37129f6d264c30615518bceeca13f))
* **retrieve:** skip sparse prefetch on dense-only collections (closes [#208](https://github.com/ericmey/musubi/issues/208)) ([#212](https://github.com/ericmey/musubi/issues/212)) ([98c658e](https://github.com/ericmey/musubi/commit/98c658e7afe270f70063d9cd3586dd3bdedad3a0))
* **synthesis:** substitute generic rationale when LLM returns empty ([#340](https://github.com/ericmey/musubi/issues/340)) ([59f7610](https://github.com/ericmey/musubi/commit/59f76104bae0a64330e487a57945e68d09ade544))


### Refactors

* **model:** agent-as-tenant namespace convention (ADR 0030) ([#255](https://github.com/ericmey/musubi/issues/255)) ([f31f073](https://github.com/ericmey/musubi/commit/f31f073e425db1d9151d4525366ea5d0b79e8c90))


### Documentation

* **adr:** 0020 python-multipart ([#89](https://github.com/ericmey/musubi/issues/89)) ([1e3a66c](https://github.com/ericmey/musubi/commit/1e3a66ce8bf09fa8ca33b6e96bffedcc1ee399bb))
* **adr:** 0022 extension ecosystem — language-based monorepo boundary ([#97](https://github.com/ericmey/musubi/issues/97)) ([891edac](https://github.com/ericmey/musubi/commit/891edac31bc186d56f12d592652c377c7bd3fc00))
* **agents:** add mergeStateStatus lesson to GEMINI.md ([#76](https://github.com/ericmey/musubi/issues/76)) ([63a4eef](https://github.com/ericmey/musubi/commit/63a4eef16941b920157c0b2668bbfeb1c61f8366))
* **api:** clarify include_archived mode-scope, harden state_filter test ([#274](https://github.com/ericmey/musubi/issues/274)) ([8c1b1db](https://github.com/ericmey/musubi/commit/8c1b1dbeb66575e96cd404dc66c93407c7fb73b0))
* **deploy:** ADR-0019 phase-gated LLM placement + spec update ([#66](https://github.com/ericmey/musubi/issues/66)) ([5415e33](https://github.com/ericmey/musubi/commit/5415e33c41df15492af181168a7a04233845a7d8))
* **gemini:** capture lessons from PR [#51](https://github.com/ericmey/musubi/issues/51) ([#56](https://github.com/ericmey/musubi/issues/56)) ([54b2352](https://github.com/ericmey/musubi/commit/54b235268afecabea4e582eeeaaa1923b5ff5fd6))
* **interfaces:** canonical agent-tools surface + ADR 0032 ([#289](https://github.com/ericmey/musubi/issues/289)) ([1529764](https://github.com/ericmey/musubi/commit/1529764bf3940cfb65279836e3e69d878ffb0ecb))
* **interfaces:** v1.0 alignment — scope table, cross-plane retrieve, adapter rewrites ([#234](https://github.com/ericmey/musubi/issues/234)) ([6c035f4](https://github.com/ericmey/musubi/commit/6c035f4350add8aea87cdad39130cdd63a4da6f3))
* **operator:** promote handoff-audit.py to top of backlog ([d4cb963](https://github.com/ericmey/musubi/commit/d4cb9634183530189949e42e06c1cc376b697524))
* **process:** codify before-handoff checks + linked-Issue rule ([#48](https://github.com/ericmey/musubi/issues/48)) ([21d08e4](https://github.com/ericmey/musubi/commit/21d08e49c6f401461e3bd9dd392216bae7e21302))
* **process:** reviewer notes convention — where Should-fix lands ([#44](https://github.com/ericmey/musubi/issues/44)) ([6fe724e](https://github.com/ericmey/musubi/commit/6fe724e80793856a7024619043ea8a49c17058c0))
* **roadmap:** add next-up.md — rolling 2-week plan post-v1.2 ([#277](https://github.com/ericmey/musubi/issues/277)) ([88f1e20](https://github.com/ericmey/musubi/commit/88f1e202fbe60e00999aa8c2547b8a4616336a2f))
* **roadmap:** add public docs wiki backlog ([#281](https://github.com/ericmey/musubi/issues/281)) ([288b952](https://github.com/ericmey/musubi/commit/288b9520601d5d97db304d846de6a02136ac0a35))
* **roadmap:** W1.2 pre-rank scoring — architecture + plan ([#280](https://github.com/ericmey/musubi/issues/280)) ([fcba068](https://github.com/ericmey/musubi/commit/fcba0687fb6f1b70a3ba86ada6f41c8f0990b3bb))
* scrub internal V1/V2 framing from user-facing docs ([#189](https://github.com/ericmey/musubi/issues/189)) ([cc97fc1](https://github.com/ericmey/musubi/commit/cc97fc199b99073b260a7a897919c2711fab604e))
* **slice-ops-observability:** record v1.3.2 deploy + OTEL activation ([#307](https://github.com/ericmey/musubi/issues/307)) ([ce26ac0](https://github.com/ericmey/musubi/commit/ce26ac0d4ee6641a6fe5f3e6df1e18a43b960efe))
* **slice:** carve slice-api-app-bootstrap ([#123](https://github.com/ericmey/musubi/issues/123)) — critical-path production bootstrap ([#124](https://github.com/ericmey/musubi/issues/124)) ([f6c4143](https://github.com/ericmey/musubi/commit/f6c4143b18c5be860908f525100637910d0ec621))
* **slice:** carve slice-api-thoughts-stream + spec endpoint contract ([#103](https://github.com/ericmey/musubi/issues/103)) ([908c0d1](https://github.com/ericmey/musubi/commit/908c0d19feb57672a97633b1cbc2a503bb51ec71))
* **slices:** carve slice-ops-first-deploy for Codex ([#117](https://github.com/ericmey/musubi/issues/117)) ([2e9ade9](https://github.com/ericmey/musubi/commit/2e9ade9b969e90b74c74018aee7bb90f0a1fdb01))
* **slices:** clear hidden pile — 4 Phase 2 slices carved + cleanup ([#112](https://github.com/ericmey/musubi/issues/112)) ([892e5f5](https://github.com/ericmey/musubi/commit/892e5f5bd50c883e4aa3aa6171ae714e229e667b))
* **vault:** capture the 2026-04-20 first-deploy milestone ([#149](https://github.com/ericmey/musubi/issues/149)) ([3b9394b](https://github.com/ericmey/musubi/commit/3b9394b7918432865e89c9688f7f9c6f58be5125))


### CI / Ops

* **ops:** Tier 1 supply-chain — cosign signing, SBOM, Trivy CVE gate ([#159](https://github.com/ericmey/musubi/issues/159)) ([0b997ee](https://github.com/ericmey/musubi/commit/0b997eeb73cf06401af66b768c020cd1f0594086))
* **ops:** Tier 2 — release-please auto-tags from conventional commits ([#160](https://github.com/ericmey/musubi/issues/160)) ([ae7e227](https://github.com/ericmey/musubi/commit/ae7e2273196598bd2c32fe1b2ab8b465e192a8b4))
* **ops:** Tier 3 — auto-bump the group_vars digest pin on every release ([#182](https://github.com/ericmey/musubi/issues/182)) ([d6f8a91](https://github.com/ericmey/musubi/commit/d6f8a91ec31245e01911a9e8e4ac3f911404984f))
* **release-please:** auto-regen uv.lock after version bump (closes [#360](https://github.com/ericmey/musubi/issues/360)) ([#364](https://github.com/ericmey/musubi/issues/364)) ([1c933c8](https://github.com/ericmey/musubi/commit/1c933c8739e63df7c7a0813626795d621bd1d1bd))
* **vault:** catch merge-time status drift at PR time ([#186](https://github.com/ericmey/musubi/issues/186)) ([e8ae852](https://github.com/ericmey/musubi/commit/e8ae8524dd77f9aa07fc3d4c38ba979787728883))
* **vault:** wire GH_TOKEN so Issue-drift check runs in CI (closes [#46](https://github.com/ericmey/musubi/issues/46)) ([#47](https://github.com/ericmey/musubi/issues/47)) ([b27f394](https://github.com/ericmey/musubi/commit/b27f394d01f5cfd87f6ba7cb1aa6604705d25eb4))

## [1.11.7](https://github.com/ericmey/musubi/compare/v1.11.6...v1.11.7) (2026-07-13)


### Bug Fixes

* **api:** enforce form/path namespace scope, operator-only fleet contradiction reads, and single-worker safety ([#403](https://github.com/ericmey/musubi/pull/403)) ([0def0df](https://github.com/ericmey/musubi/commit/0def0dff52674fc006ae3bf0750a91dc390c87eb))
* **api:** bind idempotent replay to the authorized principal, operation, namespace, and body while serializing concurrent writes ([#414](https://github.com/ericmey/musubi/pull/414)) ([8167202](https://github.com/ericmey/musubi/commit/81672020ce4478958ad766fd37433fcfbdf22d3d))
* **deploy:** target main for image bump and rollback ([#415](https://github.com/ericmey/musubi/pull/415)) ([cb8f4a0](https://github.com/ericmey/musubi/commit/cb8f4a08dd694e3545f5c7048f6a8640475316c7))

## [1.11.6](https://github.com/ericmey/musubi/compare/v1.11.5...v1.11.6) (2026-07-11)


### Bug Fixes

* **api:** a corrupted memory must be removable, must not be creatable, and retraction must keep working ([#398](https://github.com/ericmey/musubi/issues/398)) ([4f574da](https://github.com/ericmey/musubi/commit/4f574da285f8e3a3854e0d84c09d70212dbda026))

## [1.11.5](https://github.com/ericmey/musubi/compare/v1.11.4...v1.11.5) (2026-06-29)


### Bug Fixes

* **deploy:** refresh env during image updates ([#395](https://github.com/ericmey/musubi/issues/395)) ([cb09a51](https://github.com/ericmey/musubi/commit/cb09a51031c1852674f0eeda3822aaf2fc44162b))

## [1.11.4](https://github.com/ericmey/musubi/compare/v1.11.3...v1.11.4) (2026-06-29)


### Bug Fixes

* **api:** default episodic typed tags ([#392](https://github.com/ericmey/musubi/issues/392)) ([09a0d73](https://github.com/ericmey/musubi/commit/09a0d73037a31064c041ae8d7caa0bf7b9d3cb9e))

## [1.11.3](https://github.com/ericmey/musubi/compare/v1.11.2...v1.11.3) (2026-06-29)


### Bug Fixes

* **adapters:** add typed episode tags to captures ([#386](https://github.com/ericmey/musubi/issues/386)) ([1663e20](https://github.com/ericmey/musubi/commit/1663e208fee3211018281b4f1cda3123a2ff6327))

## [1.11.2](https://github.com/ericmey/musubi/compare/v1.11.1...v1.11.2) (2026-06-29)


### Bug Fixes

* **auth:** make recursive scope read-only ([#387](https://github.com/ericmey/musubi/issues/387)) ([3fd8dbd](https://github.com/ericmey/musubi/commit/3fd8dbd175294c5419b2d447b69e524675b7daf1))

## [1.11.1](https://github.com/ericmey/musubi/compare/v1.11.0...v1.11.1) (2026-06-29)


### Bug Fixes

* **ops:** expose service version in status ([#383](https://github.com/ericmey/musubi/issues/383)) ([a081501](https://github.com/ericmey/musubi/commit/a0815013c7335037005194e75da6264077f148f0))

## [1.11.0](https://github.com/ericmey/musubi/compare/v1.10.4...v1.11.0) (2026-06-28)


### Features

* **retrieve:** add context-pack endpoint ([#379](https://github.com/ericmey/musubi/issues/379)) ([6bb0591](https://github.com/ericmey/musubi/commit/6bb059112ff9fb6e0c73670d4213465d30aed819))

## [1.10.4](https://github.com/ericmey/musubi/compare/v1.10.3...v1.10.4) (2026-06-07)


### Bug Fixes

* **image:** patch gnutls CRITICAL CVEs + retry HF tokenizer prefetch ([#375](https://github.com/ericmey/musubi/issues/375)) ([f34a7df](https://github.com/ericmey/musubi/commit/f34a7df2558209e9eb99cf8b6de42093b0155484))

## [1.10.3](https://github.com/ericmey/musubi/compare/v1.10.2...v1.10.3) (2026-06-07)


### Bug Fixes

* **mcp:** musubi_get 2-part namespace foot-gun + wire musubi_recent ([#288](https://github.com/ericmey/musubi/issues/288)) ([#373](https://github.com/ericmey/musubi/issues/373)) ([1df9e62](https://github.com/ericmey/musubi/commit/1df9e620907e7654c40d086fe10fdcb4108ef47f))

## [1.10.2](https://github.com/ericmey/musubi/compare/v1.10.1...v1.10.2) (2026-05-18)


### Bug Fixes

* **lifecycle:** wrap embedder in ChunkedEmbedder (closes [#367](https://github.com/ericmey/musubi/issues/367)) ([#369](https://github.com/ericmey/musubi/issues/369)) ([e7c781d](https://github.com/ericmey/musubi/commit/e7c781de6ad04ac0aa53597c80e6802429891f6f))

## [1.10.1](https://github.com/ericmey/musubi/compare/v1.10.0...v1.10.1) (2026-05-18)


### Bug Fixes

* **curated:** same-id new-body is an UPDATE, not supersession (closes [#362](https://github.com/ericmey/musubi/issues/362)) ([#363](https://github.com/ericmey/musubi/issues/363)) ([9ed6f68](https://github.com/ericmey/musubi/commit/9ed6f6848f1fb3f6a123c59a6979716c6cdb294c))


### CI / Ops

* **release-please:** auto-regen uv.lock after version bump (closes [#360](https://github.com/ericmey/musubi/issues/360)) ([#364](https://github.com/ericmey/musubi/issues/364)) ([1c933c8](https://github.com/ericmey/musubi/commit/1c933c8739e63df7c7a0813626795d621bd1d1bd))

## [1.10.0](https://github.com/ericmey/musubi/compare/v1.9.2...v1.10.0) (2026-05-18)


### Features

* **vault:** wire vault_reconcile into lifecycle scheduler (partial musubi[#345](https://github.com/ericmey/musubi/issues/345)) ([#357](https://github.com/ericmey/musubi/issues/357)) ([eb49125](https://github.com/ericmey/musubi/commit/eb4912557807ddf332f8453e522fb5459ca3fdf8))

## [1.9.2](https://github.com/ericmey/musubi/compare/v1.9.1...v1.9.2) (2026-05-18)


### Bug Fixes

* **api,test:** clarify trigger-synthesis namespace semantics ([#354](https://github.com/ericmey/musubi/issues/354)) ([4cb9739](https://github.com/ericmey/musubi/commit/4cb9739538a688fc61e30ba075851d034f5d33d8))

## [1.9.1](https://github.com/ericmey/musubi/compare/v1.9.0...v1.9.1) (2026-05-18)


### Bug Fixes

* **ci:** skip Integration on release-please PRs ([#350](https://github.com/ericmey/musubi/issues/350)) ([8b7db98](https://github.com/ericmey/musubi/commit/8b7db987a0f326cd95ec9e9e0bcd87d87a03b29d))

## [1.9.0](https://github.com/ericmey/musubi/compare/v1.8.0...v1.9.0) (2026-05-18)


### Features

* **observability:** lifecycle-worker /metrics exposition ([#346](https://github.com/ericmey/musubi/issues/346)) ([de71ba7](https://github.com/ericmey/musubi/commit/de71ba70f719aa51d4e8cc1356f36d1bbecfd6c2))

## [1.8.0](https://github.com/ericmey/musubi/compare/v1.7.1...v1.8.0) (2026-05-18)


### Features

* **retrieve:** mode=recent (slice-retrieve-recent) ([#343](https://github.com/ericmey/musubi/issues/343)) ([6695f5d](https://github.com/ericmey/musubi/commit/6695f5d86c40335cd699ad6bbab10baddbc63501))

## [1.7.1](https://github.com/ericmey/musubi/compare/v1.7.0...v1.7.1) (2026-05-17)


### Bug Fixes

* **synthesis:** substitute generic rationale when LLM returns empty ([#340](https://github.com/ericmey/musubi/issues/340)) ([59f7610](https://github.com/ericmey/musubi/commit/59f76104bae0a64330e487a57945e68d09ade544))

## [1.7.0](https://github.com/ericmey/musubi/compare/v1.6.0...v1.7.0) (2026-05-17)


### Features

* **lifecycle:** per-family synthesis with candidates pool ([#335](https://github.com/ericmey/musubi/issues/335)) ([e2b8377](https://github.com/ericmey/musubi/commit/e2b83775dc2af9d47d03e9137d064ca7f5f708b4))
* **retrieve:** federate hybrid retrieval by identity_family ([#334](https://github.com/ericmey/musubi/issues/334)) ([61c99ae](https://github.com/ericmey/musubi/commit/61c99aebda3d5203ed11127fee93877bf1815dc5))

## [1.6.0](https://github.com/ericmey/musubi/compare/v1.5.4...v1.6.0) (2026-05-17)


### Features

* **types:** add identity_family field for cross-substrate federation ([#332](https://github.com/ericmey/musubi/issues/332)) ([7d032a9](https://github.com/ericmey/musubi/commit/7d032a99ce3da7c642d761f0452ac5de4d93af20))

## [1.5.4](https://github.com/ericmey/musubi/compare/v1.5.3...v1.5.4) (2026-05-17)


### Bug Fixes

* **embedding:** raise TEI client char-truncate from 2048 to 32000 ([#330](https://github.com/ericmey/musubi/issues/330)) ([0a33d7b](https://github.com/ericmey/musubi/commit/0a33d7b0e5decd410bbf69e9e4a7cca33eab8ca3))

## [1.5.3](https://github.com/ericmey/musubi/compare/v1.5.2...v1.5.3) (2026-05-17)


### Bug Fixes

* **deploy:** bake tokenizers into image, fix /app cache permissions ([#327](https://github.com/ericmey/musubi/issues/327)) ([7dd406a](https://github.com/ericmey/musubi/commit/7dd406aa28dc29b154711294ab1667ccb93557e1))

## [1.5.2](https://github.com/ericmey/musubi/compare/v1.5.1...v1.5.2) (2026-05-17)


### Bug Fixes

* **embedding:** chunk + max-pool sparse inputs over SPLADE's 512-token cap ([#324](https://github.com/ericmey/musubi/issues/324)) ([3c31a23](https://github.com/ericmey/musubi/commit/3c31a23e395045d998a164f371942825b88fb269))

## [1.5.1](https://github.com/ericmey/musubi/compare/v1.5.0...v1.5.1) (2026-05-15)


### Bug Fixes

* **embedding:** rebuild TEI httpx.AsyncClient per asyncio loop ([#321](https://github.com/ericmey/musubi/issues/321)) ([7a66a03](https://github.com/ericmey/musubi/commit/7a66a03ec8dc6c2a1ecc7e9bf433cd19036443d5))

## [1.5.0](https://github.com/ericmey/musubi/compare/v1.4.0...v1.5.0) (2026-05-15)


### Features

* **lifecycle:** wire OTel tracing in lifecycle-worker ([#317](https://github.com/ericmey/musubi/issues/317)) ([ff75a68](https://github.com/ericmey/musubi/commit/ff75a68659355cfd6060fe45f93c0a650d69367a))

## [1.4.0](https://github.com/ericmey/musubi/compare/v1.3.4...v1.4.0) (2026-05-15)


### Features

* **observability:** scrape qdrant metrics via Bearer-authed Prometheus job ([#314](https://github.com/ericmey/musubi/issues/314)) ([9d1ddeb](https://github.com/ericmey/musubi/commit/9d1ddeb49b5e801383b43379cc911639e3f4da8b))

## [1.3.4](https://github.com/ericmey/musubi/compare/v1.3.3...v1.3.4) (2026-05-14)


### Bug Fixes

* **llm:** constrain Ollama decoding with Pydantic JSON Schema ([#311](https://github.com/ericmey/musubi/issues/311)) ([0909a93](https://github.com/ericmey/musubi/commit/0909a93dd0654b43d6fc4e22cf2f120f78fae47b))

## [1.3.3](https://github.com/ericmey/musubi/compare/v1.3.2...v1.3.3) (2026-05-14)


### Documentation

* **slice-ops-observability:** record v1.3.2 deploy + OTEL activation ([#307](https://github.com/ericmey/musubi/issues/307)) ([ce26ac0](https://github.com/ericmey/musubi/commit/ce26ac0d4ee6641a6fe5f3e6df1e18a43b960efe))

## [1.3.2](https://github.com/ericmey/musubi/compare/v1.3.1...v1.3.2) (2026-05-14)


### Bug Fixes

* **observability:** complete server-side OTel tracing (debt from slice-ops-observability) ([#303](https://github.com/ericmey/musubi/issues/303)) ([b04c3b1](https://github.com/ericmey/musubi/commit/b04c3b1ffcc2f7289b156aab5c3192955d0f286d))

## [1.3.1](https://github.com/ericmey/musubi/compare/v1.3.0...v1.3.1) (2026-05-04)


### Documentation

* **roadmap:** W1.2 pre-rank scoring — architecture + plan ([#280](https://github.com/ericmey/musubi/issues/280)) ([fcba068](https://github.com/ericmey/musubi/commit/fcba0687fb6f1b70a3ba86ada6f41c8f0990b3bb))

## [1.3.0](https://github.com/ericmey/musubi/compare/v1.2.4...v1.3.0) (2026-04-30)


### Features

* **adapters/mcp:** slice-mcp-canonical-tools — canonical 5-tool surface ([#292](https://github.com/ericmey/musubi/issues/292)) ([e1dec86](https://github.com/ericmey/musubi/commit/e1dec867ddfb6712db02d1eedd66cc2cd9b4f9f7))

## [1.2.4](https://github.com/ericmey/musubi/compare/v1.2.3...v1.2.4) (2026-04-30)


### Documentation

* **interfaces:** canonical agent-tools surface + ADR 0032 ([#289](https://github.com/ericmey/musubi/issues/289)) ([1529764](https://github.com/ericmey/musubi/commit/1529764bf3940cfb65279836e3e69d878ffb0ecb))

## [1.2.3](https://github.com/ericmey/musubi/compare/v1.2.2...v1.2.3) (2026-04-25)


### Documentation

* **roadmap:** add public docs wiki backlog ([#281](https://github.com/ericmey/musubi/issues/281)) ([288b952](https://github.com/ericmey/musubi/commit/288b9520601d5d97db304d846de6a02136ac0a35))

## [1.2.2](https://github.com/ericmey/musubi/compare/v1.2.1...v1.2.2) (2026-04-25)


### Documentation

* **roadmap:** add next-up.md — rolling 2-week plan post-v1.2 ([#277](https://github.com/ericmey/musubi/issues/277)) ([88f1e20](https://github.com/ericmey/musubi/commit/88f1e202fbe60e00999aa8c2547b8a4616336a2f))

## [1.2.1](https://github.com/ericmey/musubi/compare/v1.2.0...v1.2.1) (2026-04-25)


### Documentation

* **api:** clarify include_archived mode-scope, harden state_filter test ([#274](https://github.com/ericmey/musubi/issues/274)) ([8c1b1db](https://github.com/ericmey/musubi/commit/8c1b1dbeb66575e96cd404dc66c93407c7fb73b0))

## [1.2.0](https://github.com/ericmey/musubi/compare/v1.1.0...v1.2.0) (2026-04-25)


### Features

* **api:** expose state_filter on POST /v1/retrieve body ([#271](https://github.com/ericmey/musubi/issues/271)) ([e34fffe](https://github.com/ericmey/musubi/commit/e34fffe4f6bffafd575d52d954a8d995c08171e1))

## [1.1.0](https://github.com/ericmey/musubi/compare/v1.0.0...v1.1.0) (2026-04-24)


### Features

* **api:** wildcard namespace segments for tenant-wide retrieve ([#268](https://github.com/ericmey/musubi/issues/268)) ([e9ef0be](https://github.com/ericmey/musubi/commit/e9ef0befb576ec49ddbc299da4ba5ba755df86f4))

## [1.0.0](https://github.com/ericmey/musubi/compare/v0.8.3...v1.0.0) (2026-04-24)


### Features

* release v1.0 — API sealed, integrations on canonical, automation hands-off ([#264](https://github.com/ericmey/musubi/issues/264)) ([92fab8b](https://github.com/ericmey/musubi/commit/92fab8bc202f5b273638f111ac53a5d284345ae5))

## [0.8.3](https://github.com/ericmey/musubi/compare/v0.8.2...v0.8.3) (2026-04-24)


### Bug Fixes

* **deploy:** remove stale per-version commentary on the core-image pin ([#260](https://github.com/ericmey/musubi/issues/260)) ([7acd357](https://github.com/ericmey/musubi/commit/7acd35714ecff5959cc005e758447c0540d8beb8))

## [0.8.2](https://github.com/ericmey/musubi/compare/v0.8.1...v0.8.2) (2026-04-24)


### Bug Fixes

* **docs:** path-prefix the ADR-0028 wikilink in ADR 0030 ([#257](https://github.com/ericmey/musubi/issues/257)) ([baaab32](https://github.com/ericmey/musubi/commit/baaab3259da182315aa645c87c6a7176625e8b5f))

## [0.8.1](https://github.com/ericmey/musubi/compare/v0.8.0...v0.8.1) (2026-04-24)


### Refactors

* **model:** agent-as-tenant namespace convention (ADR 0030) ([#255](https://github.com/ericmey/musubi/issues/255)) ([f31f073](https://github.com/ericmey/musubi/commit/f31f073e425db1d9151d4525366ea5d0b79e8c90))

## [0.8.0](https://github.com/ericmey/musubi/compare/v0.7.1...v0.8.0) (2026-04-24)


### Features

* **ci:** auto-enable auto-merge on auto-digest-bump PRs ([#252](https://github.com/ericmey/musubi/issues/252)) ([e44abd0](https://github.com/ericmey/musubi/commit/e44abd076846fe14b87415a2b9ab090b328abb10))

## [0.7.1](https://github.com/ericmey/musubi/compare/v0.7.0...v0.7.1) (2026-04-24)


### Bug Fixes

* **ci:** auto-digest-bump trigger + auth (closes [#247](https://github.com/ericmey/musubi/issues/247)) ([#249](https://github.com/ericmey/musubi/issues/249)) ([8540a3c](https://github.com/ericmey/musubi/commit/8540a3ca7a2bff966841ab848ccd2223d57df025))

## [0.7.0](https://github.com/ericmey/musubi/compare/v0.6.1...v0.7.0) (2026-04-24)


### Features

* **ci:** auto-enable auto-merge on release-please PRs ([#245](https://github.com/ericmey/musubi/issues/245)) ([774f5e7](https://github.com/ericmey/musubi/commit/774f5e7f8682cef71c2e5dbe5717450dd62495bb))

## [0.6.1](https://github.com/ericmey/musubi/compare/v0.6.0...v0.6.1) (2026-04-24)


### Bug Fixes

* **ci:** grant contents:write so release-body append can update release (closes [#242](https://github.com/ericmey/musubi/issues/242)) ([#243](https://github.com/ericmey/musubi/issues/243)) ([b98215c](https://github.com/ericmey/musubi/commit/b98215cb6621eb2afa3862a0a33c38fe25fcd37b))

## [0.6.0](https://github.com/ericmey/musubi/compare/v0.5.1...v0.6.0) (2026-04-24)


### Features

* **api:** implement Last-Event-ID replay on /v1/thoughts/stream ([#238](https://github.com/ericmey/musubi/issues/238)) ([f718116](https://github.com/ericmey/musubi/commit/f718116c18b70223af5408f10884d29ffc0af634))
* **api:** promote `title` to top-level on RetrieveResultRow (breaking) ([#236](https://github.com/ericmey/musubi/issues/236)) ([5e79151](https://github.com/ericmey/musubi/commit/5e79151f176879e452f112fa78762aa85bc26b5b))
* **api:** rename endpoints to plane-aligned paths (breaking, v1.0) ([#237](https://github.com/ericmey/musubi/issues/237)) ([ea16b3d](https://github.com/ericmey/musubi/commit/ea16b3d477feee32e4f9bc61f11d0306a9a050d7))
* **cli:** operator CLI for promote force / reject ([#227](https://github.com/ericmey/musubi/issues/227)) ([b928739](https://github.com/ericmey/musubi/commit/b928739edb9785dc6599a0bccc6dbc0c8a4f0247))
* **lifecycle:** artifact archival sweep (opt-in) + slice-ops-storage ([#226](https://github.com/ericmey/musubi/issues/226)) ([50f0c59](https://github.com/ericmey/musubi/commit/50f0c5947da683e19372f99068be35d3bab19f39))
* **vault:** event rate limit + indexing concurrency cap ([#229](https://github.com/ericmey/musubi/issues/229)) ([6950422](https://github.com/ericmey/musubi/commit/6950422c89715360cad437db16dbde0be74fb9d5))
* **vault:** skip-with-warning for binary + oversize files in sync layer ([#228](https://github.com/ericmey/musubi/issues/228)) ([4cf436d](https://github.com/ericmey/musubi/commit/4cf436d4fa7e2180827f4b3b14b0666b3f4a4816))


### Bug Fixes

* **ci:** release-please tag push must use a PAT, not GITHUB_TOKEN ([#215](https://github.com/ericmey/musubi/issues/215)) ([a775581](https://github.com/ericmey/musubi/commit/a775581df12e7bba4534ec8c95d27dce8ade69a4))
* **lifecycle:** wire promotion_attempts gate + last_reinforced_at reinforce clock ([#225](https://github.com/ericmey/musubi/issues/225)) ([58eea81](https://github.com/ericmey/musubi/commit/58eea812f17fe2e0beca41d892728a6bad93b2bd))


### Documentation

* **interfaces:** v1.0 alignment — scope table, cross-plane retrieve, adapter rewrites ([#234](https://github.com/ericmey/musubi/issues/234)) ([6c035f4](https://github.com/ericmey/musubi/commit/6c035f4350add8aea87cdad39130cdd63a4da6f3))

## [0.5.1](https://github.com/ericmey/musubi/compare/v0.5.0...v0.5.1) (2026-04-23)


### Bug Fixes

* **retrieve:** skip sparse prefetch on dense-only collections (closes [#208](https://github.com/ericmey/musubi/issues/208)) ([#212](https://github.com/ericmey/musubi/issues/212)) ([98c658e](https://github.com/ericmey/musubi/commit/98c658e7afe270f70063d9cd3586dd3bdedad3a0))

## [0.5.0](https://github.com/ericmey/musubi/compare/v0.4.0...v0.5.0) (2026-04-23)


### Features

* **retrieve:** 2-segment namespace + cross-plane fanout, strict scope (closes [#209](https://github.com/ericmey/musubi/issues/209)) ([#210](https://github.com/ericmey/musubi/issues/210)) ([1d9c2e1](https://github.com/ericmey/musubi/commit/1d9c2e1f17330e7e91cd3180aeea44e1ae483224))


### Bug Fixes

* **rate-limit:** delete dead settings-backed rate fields (closes [#193](https://github.com/ericmey/musubi/issues/193)) ([#206](https://github.com/ericmey/musubi/issues/206)) ([b8e98d5](https://github.com/ericmey/musubi/commit/b8e98d53c4e37129f6d264c30615518bceeca13f))

## [0.4.0](https://github.com/ericmey/musubi/compare/v0.3.1...v0.4.0) (2026-04-23)


### Features

* **api,sdk:** operator-gated created_at override on capture (closes [#140](https://github.com/ericmey/musubi/issues/140)) ([#203](https://github.com/ericmey/musubi/issues/203)) ([a668560](https://github.com/ericmey/musubi/commit/a668560e2b09b8485f8079ba1f6b3d2d94f1ae7d))
* **ops:** perf-testing harness + manual-recovery runbook (Gate 0) ([#191](https://github.com/ericmey/musubi/issues/191)) ([0976622](https://github.com/ericmey/musubi/commit/0976622f8b30890185015bcc39841f9c893c6c96))
* **plane:** batch_create + longer-wins merge strategy (closes [#141](https://github.com/ericmey/musubi/issues/141), [#142](https://github.com/ericmey/musubi/issues/142)) ([#204](https://github.com/ericmey/musubi/issues/204)) ([eeaae56](https://github.com/ericmey/musubi/commit/eeaae5681e323c302c6846b12c5713e759957ffb))


### Bug Fixes

* **api:** map pydantic ValidationError to 422 (closes [#192](https://github.com/ericmey/musubi/issues/192)) ([#201](https://github.com/ericmey/musubi/issues/201)) ([6196283](https://github.com/ericmey/musubi/commit/6196283be49ac7b39f9ed8d6d325a62f73fbfccb))
* **api:** wire POST /v1/retrieve to the real orchestration pipeline ([#197](https://github.com/ericmey/musubi/issues/197)) ([9f80644](https://github.com/ericmey/musubi/commit/9f8064491aba851f2850d846a2b530b2ddfdece3))
* **embedding:** reuse a pooled httpx.AsyncClient per TEI client ([#198](https://github.com/ericmey/musubi/issues/198)) ([5a24e28](https://github.com/ericmey/musubi/commit/5a24e280906db65b26d73e17c6332466e6e72dbc))
* **ops:** perf harness — idempotent seed + remote telemetry + 429 backoff ([#194](https://github.com/ericmey/musubi/issues/194)) ([85cf5be](https://github.com/ericmey/musubi/commit/85cf5be506ef41d41cff889594f722bf3789c745))
* **ops:** perf-harness round 3 — fixes found during live Gate 1 run ([#196](https://github.com/ericmey/musubi/issues/196)) ([1031104](https://github.com/ericmey/musubi/commit/1031104d0e838d6a02a704597bc752d017448451))
* **ops:** repair backup/restore playbook (closes [#190](https://github.com/ericmey/musubi/issues/190)) ([#202](https://github.com/ericmey/musubi/issues/202)) ([63e0765](https://github.com/ericmey/musubi/commit/63e0765c6df0adc3c9d4ab89a70f475a69b8ee84))


### Documentation

* scrub internal V1/V2 framing from user-facing docs ([#189](https://github.com/ericmey/musubi/issues/189)) ([cc97fc1](https://github.com/ericmey/musubi/commit/cc97fc199b99073b260a7a897919c2711fab604e))


### CI / Ops

* **vault:** catch merge-time status drift at PR time ([#186](https://github.com/ericmey/musubi/issues/186)) ([e8ae852](https://github.com/ericmey/musubi/commit/e8ae8524dd77f9aa07fc3d4c38ba979787728883))

## [0.3.1](https://github.com/ericmey/musubi/compare/v0.3.0...v0.3.1) (2026-04-21)


### Bug Fixes

* **ansible:** restore inventory.yml indentation ([#178](https://github.com/ericmey/musubi/issues/178)) ([0cdb065](https://github.com/ericmey/musubi/commit/0cdb0652c05a8e2dd131bfbdbc67835b311cca81))
* **lifecycle:** stop registering demotion jobs from build_maturation_jobs ([#181](https://github.com/ericmey/musubi/issues/181)) ([da0f2e9](https://github.com/ericmey/musubi/commit/da0f2e91e9a0be33e629b1df9f92952d711acdb6))


### CI / Ops

* **ops:** Tier 3 — auto-bump the group_vars digest pin on every release ([#182](https://github.com/ericmey/musubi/issues/182)) ([d6f8a91](https://github.com/ericmey/musubi/commit/d6f8a91ec31245e01911a9e8e4ac3f911404984f))

## [0.3.0](https://github.com/ericmey/musubi/compare/v0.2.0...v0.3.0) (2026-04-21)


### Features

* **_operator:** merge-flow.py — automate per-slice post-merge ritual ([#100](https://github.com/ericmey/musubi/issues/100)) ([278d354](https://github.com/ericmey/musubi/commit/278d3543760386bbf825ef6a456a6293c24e30ce))
* **_operator:** musubi slice status board ([#105](https://github.com/ericmey/musubi/issues/105)) ([a10bcd1](https://github.com/ericmey/musubi/commit/a10bcd12b0bba365536ba6a112cbc0598c5dacfe))
* **_operator:** render-prompt ([#122](https://github.com/ericmey/musubi/issues/122)) ([0b0fdce](https://github.com/ericmey/musubi/commit/0b0fdce07177cbd9c9964e5b751db577c8679b28))
* **adapter-livekit:** slice-adapter-livekit ([#96](https://github.com/ericmey/musubi/issues/96)) ([90d81bb](https://github.com/ericmey/musubi/commit/90d81bbde822cf4eb38cc133589522e35b01854f))
* **adapters:** slice-adapter-mcp ([#95](https://github.com/ericmey/musubi/issues/95)) ([d59afcd](https://github.com/ericmey/musubi/commit/d59afcd0e604df4c595cbdfd4329a8f9c53fc42d))
* **api:** debug-synthesis-trigger endpoint + [#119](https://github.com/ericmey/musubi/issues/119) ollama-offline unskip ([#139](https://github.com/ericmey/musubi/issues/139)) ([826751d](https://github.com/ericmey/musubi/commit/826751d82331a1136a8673c5d4541967933b73a4))
* **api:** slice-api-app-bootstrap ([#126](https://github.com/ericmey/musubi/issues/126)) ([72fc825](https://github.com/ericmey/musubi/commit/72fc825fc432d32470beb3fdc114fda3a044e8f8))
* **api:** slice-api-thoughts-stream ([#106](https://github.com/ericmey/musubi/issues/106)) ([b0865d3](https://github.com/ericmey/musubi/commit/b0865d32e8054c5451acd1cb629ce44ab6f30174))
* **api:** slice-api-v0-read ([#73](https://github.com/ericmey/musubi/issues/73)) ([7cc5576](https://github.com/ericmey/musubi/commit/7cc5576e091fc6c9d640ef2a59a6d809c307de59))
* **api:** slice-api-v0-write ([#78](https://github.com/ericmey/musubi/issues/78)) ([49b6766](https://github.com/ericmey/musubi/commit/49b67663806a4a8944cf3e53bbe5a8dc815bf670))
* **auth:** slice-auth ([#38](https://github.com/ericmey/musubi/issues/38)) ([43fd792](https://github.com/ericmey/musubi/commit/43fd792cc1f8a57ef773c26205984871fbc95098))
* **config:** settings loader for slice-config ([1d6f410](https://github.com/ericmey/musubi/commit/1d6f4105f12a9ef52c666369c9f487a71c674a3f))
* **embedding:** batching + caching + truncation (slice-embedding follow-up) ([#41](https://github.com/ericmey/musubi/issues/41)) ([1f5ee0b](https://github.com/ericmey/musubi/commit/1f5ee0bf5523fd2469fcfad7c0f368c452ed79fe))
* **embedding:** Embedder protocol, TEI clients, and FakeEmbedder for slice-embedding ([fd02c91](https://github.com/ericmey/musubi/commit/fd02c91bf558cc17e6dd89d4c1e2cc7c65b262f7))
* **ingestion:** slice-ingestion-capture ([#86](https://github.com/ericmey/musubi/issues/86)) ([754c4e9](https://github.com/ericmey/musubi/commit/754c4e9c6d73019a87de2e9fb4ca98350e640230))
* **integration:** slice-ops-integration-harness ([#114](https://github.com/ericmey/musubi/issues/114)) ([821b11f](https://github.com/ericmey/musubi/commit/821b11f3e315037547a578f18ab5f0469636e252))
* **lifecycle:** promotion-builder ([#171](https://github.com/ericmey/musubi/issues/171)) ([0d5c53b](https://github.com/ericmey/musubi/commit/0d5c53bc42140112ba3dea134d52f4cbdd7c7aab))
* **lifecycle:** reflection-builder closes P3 ([#172](https://github.com/ericmey/musubi/issues/172)) ([f327890](https://github.com/ericmey/musubi/commit/f3278908c97ed3d50834aea7f8c16afd872ca1fb))
* **lifecycle:** slice-lifecycle-engine ([#40](https://github.com/ericmey/musubi/issues/40)) ([99ac840](https://github.com/ericmey/musubi/commit/99ac840748291257e56149c31aa3978620fecbb6))
* **lifecycle:** slice-lifecycle-maturation ([#52](https://github.com/ericmey/musubi/issues/52)) ([d241fff](https://github.com/ericmey/musubi/commit/d241fffaad828062168f4dd02e91bd64f96fec5b))
* **lifecycle:** slice-lifecycle-promotion ([#68](https://github.com/ericmey/musubi/issues/68)) ([466eb63](https://github.com/ericmey/musubi/commit/466eb639b4c829528cc261214c7989ccc21d132b))
* **lifecycle:** slice-lifecycle-reflection ([#57](https://github.com/ericmey/musubi/issues/57)) ([5c4da46](https://github.com/ericmey/musubi/commit/5c4da4656af757a57966cf14bc1b2264972377ed))
* **lifecycle:** slice-lifecycle-synthesis ([#62](https://github.com/ericmey/musubi/issues/62)) ([880acc2](https://github.com/ericmey/musubi/commit/880acc219b83b4169f28f8ac25c5a0aee5255887))
* **lifecycle:** synthesis-builder ([#165](https://github.com/ericmey/musubi/issues/165)) ([035895a](https://github.com/ericmey/musubi/commit/035895a382275a8c5882150df505d3baf62a6530))
* **lifecycle:** wire real demotion jobs into the runner ([#163](https://github.com/ericmey/musubi/issues/163)) ([38db8eb](https://github.com/ericmey/musubi/commit/38db8ebd7054a93ec0a50b97c8a50f88d1f48267))
* **migration:** slice-poc-data-migration ([#128](https://github.com/ericmey/musubi/issues/128)) ([c419aad](https://github.com/ericmey/musubi/commit/c419aadff8a977f292d5705dc7fb7942db7fa131))
* **observability:** slice-ops-observability ([#104](https://github.com/ericmey/musubi/issues/104)) ([fb4cab1](https://github.com/ericmey/musubi/commit/fb4cab1797a91b6d6bca6cf537fc18dadf686e74))
* **ops:** brain + backup + Prometheus + GHCR publish + update.yml ([#154](https://github.com/ericmey/musubi/issues/154)) ([bdacc04](https://github.com/ericmey/musubi/commit/bdacc0453ca2573eb182ef8307db8a2affe64e06))
* **ops:** musubi-core Dockerfile + compose/env template fixes for real deploy ([#148](https://github.com/ericmey/musubi/issues/148)) ([05f65d7](https://github.com/ericmey/musubi/commit/05f65d7e376d6e64863cc696274654c7c8504788))
* **ops:** slice-ops-ansible ([#77](https://github.com/ericmey/musubi/issues/77)) ([9a2b0a3](https://github.com/ericmey/musubi/commit/9a2b0a38283c0a201d42feb682958572fb629a2b))
* **ops:** slice-ops-backup ([#82](https://github.com/ericmey/musubi/issues/82)) ([5e8b530](https://github.com/ericmey/musubi/commit/5e8b530b80b2111386ce7e357a57581513706fbf))
* **ops:** slice-ops-compose ([#85](https://github.com/ericmey/musubi/issues/85)) ([e71edab](https://github.com/ericmey/musubi/commit/e71edab32dbf14ec7ffe7af2b2672cac49163c8c))
* **ops:** slice-ops-first-deploy ([#121](https://github.com/ericmey/musubi/issues/121)) ([9cb8416](https://github.com/ericmey/musubi/commit/9cb8416ea216de24ba277398b8d72c3216b2ae05))
* **ops:** slice-ops-hardening-suite ([#125](https://github.com/ericmey/musubi/issues/125)) ([88e2643](https://github.com/ericmey/musubi/commit/88e26437e55d779883baeefbdb688608d29ef044))
* **planes:** first cut of the episodic plane for slice-plane-episodic ([05c1797](https://github.com/ericmey/musubi/commit/05c1797fbdf6c5964e7c1419d162aa5c1db4c7bd))
* **planes:** slice-plane-artifact ([#51](https://github.com/ericmey/musubi/issues/51)) ([daf93dd](https://github.com/ericmey/musubi/commit/daf93dd61fcb17dc4bea0f0bc035f339d106a9df))
* **planes:** slice-plane-concept ([#42](https://github.com/ericmey/musubi/issues/42)) ([4819b86](https://github.com/ericmey/musubi/commit/4819b866ad1e9d994f0661fd787903bdfa152516))
* **planes:** slice-plane-curated ([#39](https://github.com/ericmey/musubi/issues/39)) ([b307cda](https://github.com/ericmey/musubi/commit/b307cda0515bb0731977fc31d0f9b98214b100d2))
* **planes:** slice-plane-episodic-followup ([#94](https://github.com/ericmey/musubi/issues/94)) ([c91d70f](https://github.com/ericmey/musubi/commit/c91d70ff2d9e47825d74d12f8718a8e6c82df449))
* **planes:** slice-plane-thoughts ([#49](https://github.com/ericmey/musubi/issues/49)) ([1a7669e](https://github.com/ericmey/musubi/commit/1a7669e74cacdf0ff9d4d9d65dd92a0a680cf964))
* **retrieve:** slice-retrieval-blended ([#79](https://github.com/ericmey/musubi/issues/79)) ([5600b45](https://github.com/ericmey/musubi/commit/5600b45f29e80d7ccebd528b5209a8a8aa13ff93))
* **retrieve:** slice-retrieval-deep ([#67](https://github.com/ericmey/musubi/issues/67)) ([f9d665e](https://github.com/ericmey/musubi/commit/f9d665e8e206e5bc8fbbff3325699882d1f0fe49))
* **retrieve:** slice-retrieval-fast ([#74](https://github.com/ericmey/musubi/issues/74)) ([31e8c79](https://github.com/ericmey/musubi/commit/31e8c792085d4cbf4a63001f5892f0c215d43922))
* **retrieve:** slice-retrieval-hybrid ([#50](https://github.com/ericmey/musubi/issues/50)) ([667b99e](https://github.com/ericmey/musubi/commit/667b99e39fcd1c4603d860ac4d7ffe25063129dd))
* **retrieve:** slice-retrieval-orchestration ([#87](https://github.com/ericmey/musubi/issues/87)) ([8b418f9](https://github.com/ericmey/musubi/commit/8b418f912722503f3acd5f62837aff1b9eec9808))
* **retrieve:** slice-retrieval-rerank ([#60](https://github.com/ericmey/musubi/issues/60)) ([45c8400](https://github.com/ericmey/musubi/commit/45c8400d6d4ffbd51ac41338a439fa954dff6c2a))
* **retrieve:** slice-retrieval-scoring ([#54](https://github.com/ericmey/musubi/issues/54)) ([1d6a69a](https://github.com/ericmey/musubi/commit/1d6a69afc0b4436c3b6c60b5ca768a767ab1da57))
* **sdk:** slice-sdk-py ([#90](https://github.com/ericmey/musubi/issues/90)) ([3b13644](https://github.com/ericmey/musubi/commit/3b136441adb3083eeccb2f064d3ec1c68359fe99))
* **store:** slice-qdrant-layout first cut — bootstrap + indexes ([0f46281](https://github.com/ericmey/musubi/commit/0f46281e8e639fe13e06b6a84215bdfc2bc78c47))
* **tools:** tc_coverage — mechanical Test Contract closure audit; skills-README mirror pattern ([bb93dfb](https://github.com/ericmey/musubi/commit/bb93dfbede8814c8c3987b26889585f89ca2a0f5))
* **types:** slice-types first cut — pydantic foundation ([9d57c37](https://github.com/ericmey/musubi/commit/9d57c37e0e4424b6fa296cc6218d9b5f6a82f025))
* **types:** slice-types-followup ([#113](https://github.com/ericmey/musubi/issues/113)) ([038c9d6](https://github.com/ericmey/musubi/commit/038c9d67422f5886f596a6d727128268ff913474))
* **vault:** slice-vault-sync ([#64](https://github.com/ericmey/musubi/issues/64)) ([a584e46](https://github.com/ericmey/musubi/commit/a584e46256ce591c982cec437ab779057f6043dd))


### Bug Fixes

* **_operator:** accept chore(slice): handoff as valid prefix ([#99](https://github.com/ericmey/musubi/issues/99)) ([aab03b1](https://github.com/ericmey/musubi/commit/aab03b1430e02e56d776498bb6062e44b670963c))
* **_operator:** guard merge-flow.py flip + reconcile observability paths ([66d5066](https://github.com/ericmey/musubi/commit/66d5066dcfa837671e9b573e29058d5a3a253f24))
* **_operator:** merge-flow.py orphan-sweep survives auto-deleted branches ([#115](https://github.com/ericmey/musubi/issues/115)) ([029592b](https://github.com/ericmey/musubi/commit/029592b3f8974070576a9e7e296eea4ee7a33898))
* **_tools:** ast-based skip-reason scan for tc-coverage ([#91](https://github.com/ericmey/musubi/issues/91)) ([f91f938](https://github.com/ericmey/musubi/commit/f91f9386185d91ef5c943397f2faef2d93fd5b39))
* **_tools:** downgrade done slice overlap to warning ([#101](https://github.com/ericmey/musubi/issues/101)) ([f4071fd](https://github.com/ericmey/musubi/commit/f4071fd0acac55f629522509bf12b07d71295d1c))
* **_tools:** promote frontmatter/label drift to error for review/done states ([#93](https://github.com/ericmey/musubi/issues/93)) ([b0b6119](https://github.com/ericmey/musubi/commit/b0b611949175b5d1685ba345b8fafaa33e2709ef))
* **api:** fix 500 on non-trivial artifact upload ([#135](https://github.com/ericmey/musubi/issues/135)) ([a36e339](https://github.com/ericmey/musubi/commit/a36e339453e880bc989369ea69c9e930586ae1e4))
* **api:** ops/status probes ollama at /api/tags, not /health ([#153](https://github.com/ericmey/musubi/issues/153)) ([f6dc709](https://github.com/ericmey/musubi/commit/f6dc7096a5d730590b50f3fd93482a425faef046))
* **artifact:** tokenizer wiring for chunkers (rescue finish for Codex) ([#132](https://github.com/ericmey/musubi/issues/132)) ([98b4d55](https://github.com/ericmey/musubi/commit/98b4d55be98a154995633c4458bd71f9423e4d73))
* **ci:** grant actions:read so upload-sarif can fingerprint workflow run ([#167](https://github.com/ericmey/musubi/issues/167)) ([0b7eac6](https://github.com/ericmey/musubi/commit/0b7eac618dfc7a299a62d071e62d6d041eddd11e))
* **ci:** grant security-events:write + surface Trivy findings in table format ([#162](https://github.com/ericmey/musubi/issues/162)) ([3249ebd](https://github.com/ericmey/musubi/commit/3249ebd191d445db1bbd9677640cfd0dc13d8dab))
* **ci:** scope Trivy SARIF gate to CRITICAL via limit-severities-for-sarif ([#166](https://github.com/ericmey/musubi/issues/166)) ([101061a](https://github.com/ericmey/musubi/commit/101061a111d89e5116a379a23fef22e7b76a4112))
* **ci:** tolerate Security-tab upload failure on private repo ([#170](https://github.com/ericmey/musubi/issues/170)) ([b8f4f30](https://github.com/ericmey/musubi/commit/b8f4f30f2f4b4a8a351c399ced9bfb07b4b5292c))
* **ci:** trivy-action version — 0.28.0 doesn't exist; use 0.35.0 ([#161](https://github.com/ericmey/musubi/issues/161)) ([57ee32d](https://github.com/ericmey/musubi/commit/57ee32de18197633445f634f168fc5cac6b870b8))
* cold-cache retrieve delay ([#133](https://github.com/ericmey/musubi/issues/133)) ([#138](https://github.com/ericmey/musubi/issues/138)) ([cd7eab2](https://github.com/ericmey/musubi/commit/cd7eab226cae4bc2ab58055ac914949d2950eff2))
* **lifecycle:** concept_maturation_sweep skips concepts with active contradicts ([#65](https://github.com/ericmey/musubi/issues/65)) ([8b165de](https://github.com/ericmey/musubi/commit/8b165de98632f47bc70646267d37227038f796a5))
* **lifecycle:** emit worker job metrics ([#130](https://github.com/ericmey/musubi/issues/130)) ([c6235f5](https://github.com/ericmey/musubi/commit/c6235f5a14d67bcbab11cb6b064b4c4077b2809e))
* **mcp:** route adapter env reads through get_settings (v2 CI hotfix) ([618e01d](https://github.com/ericmey/musubi/commit/618e01d8cd46ab92d0e7131a57a118eb56a96b98))
* **operator:** detect file-owned paths in handoff audit ([#127](https://github.com/ericmey/musubi/issues/127)) ([82258ad](https://github.com/ericmey/musubi/commit/82258ad6bb3a87db3e95e02067bad28269652800))


### Documentation

* **adr:** 0020 python-multipart ([#89](https://github.com/ericmey/musubi/issues/89)) ([d1201c6](https://github.com/ericmey/musubi/commit/d1201c69c21db6cf0e1707e1f250330219d9c3a7))
* **adr:** 0022 extension ecosystem — language-based monorepo boundary ([#97](https://github.com/ericmey/musubi/issues/97)) ([e30a929](https://github.com/ericmey/musubi/commit/e30a929734cd4f7447eaa43047d2a03e3ba8b2bc))
* **agents:** add mergeStateStatus lesson to GEMINI.md ([#76](https://github.com/ericmey/musubi/issues/76)) ([d467c60](https://github.com/ericmey/musubi/commit/d467c60973521b8a23bbe750c201ff078ed455da))
* **deploy:** ADR-0019 phase-gated LLM placement + spec update ([#66](https://github.com/ericmey/musubi/issues/66)) ([1c075e1](https://github.com/ericmey/musubi/commit/1c075e172a09b44e89839941b78bd0782591640a))
* **gemini:** capture lessons from PR [#51](https://github.com/ericmey/musubi/issues/51) ([#56](https://github.com/ericmey/musubi/issues/56)) ([a291fec](https://github.com/ericmey/musubi/commit/a291fec44e3ada3761ea523fda1feacf722dd074))
* **operator:** promote handoff-audit.py to top of backlog ([eb72154](https://github.com/ericmey/musubi/commit/eb721543e2a34585bf5b6cb77f8c27de9d190508))
* **process:** codify before-handoff checks + linked-Issue rule ([#48](https://github.com/ericmey/musubi/issues/48)) ([ba24876](https://github.com/ericmey/musubi/commit/ba24876f31e1e0f1fab0a5f341eaad6ab1ed7e85))
* **process:** reviewer notes convention — where Should-fix lands ([#44](https://github.com/ericmey/musubi/issues/44)) ([e37f4c9](https://github.com/ericmey/musubi/commit/e37f4c91dcd49b22ef6d0a50e7dcaab0aeb7e11e))
* **slice:** carve slice-api-app-bootstrap ([#123](https://github.com/ericmey/musubi/issues/123)) — critical-path production bootstrap ([#124](https://github.com/ericmey/musubi/issues/124)) ([1d80364](https://github.com/ericmey/musubi/commit/1d803647be3e39f53cc68ae7e4cd0032926dda7b))
* **slice:** carve slice-api-thoughts-stream + spec endpoint contract ([#103](https://github.com/ericmey/musubi/issues/103)) ([5da3771](https://github.com/ericmey/musubi/commit/5da3771037f4be905626bc075ab6d7ade717f492))
* **slices:** carve slice-ops-first-deploy for Codex ([#117](https://github.com/ericmey/musubi/issues/117)) ([037edfc](https://github.com/ericmey/musubi/commit/037edfc9e9a0ece95e048898b42ceedc0faf6696))
* **slices:** clear hidden pile — 4 Phase 2 slices carved + cleanup ([#112](https://github.com/ericmey/musubi/issues/112)) ([719723f](https://github.com/ericmey/musubi/commit/719723fb9417ed5fd8dbc37bbe7ff0ca4886281f))
* **vault:** capture the 2026-04-20 first-deploy milestone ([#149](https://github.com/ericmey/musubi/issues/149)) ([f9c7aca](https://github.com/ericmey/musubi/commit/f9c7aca7d9b63c8c64ab8fdd63975c1a77ef6090))


### CI / Ops

* **ops:** Tier 1 supply-chain — cosign signing, SBOM, Trivy CVE gate ([#159](https://github.com/ericmey/musubi/issues/159)) ([436ab2d](https://github.com/ericmey/musubi/commit/436ab2d4e6568c04bffb621a9590d97e7750356b))
* **ops:** Tier 2 — release-please auto-tags from conventional commits ([#160](https://github.com/ericmey/musubi/issues/160)) ([9e2dd91](https://github.com/ericmey/musubi/commit/9e2dd91480bb60714e72b5e28f3c0407e939efcc))
* **vault:** wire GH_TOKEN so Issue-drift check runs in CI (closes [#46](https://github.com/ericmey/musubi/issues/46)) ([#47](https://github.com/ericmey/musubi/issues/47)) ([9cdf2ad](https://github.com/ericmey/musubi/commit/9cdf2adf7dd6f60a9836375a31a8f3e7a1951be1))
