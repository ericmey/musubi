# Changelog

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
