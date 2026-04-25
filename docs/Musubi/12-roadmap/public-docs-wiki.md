---
title: Public docs / wiki backlog
section: 12-roadmap
tags: [docs, roadmap, section/roadmap, status/draft, type/roadmap, wiki]
type: roadmap
status: draft
updated: 2026-04-25
up: "[[12-roadmap/index]]"
reviewed: false
---

# Public docs / wiki backlog

> Build a public-facing documentation site for Musubi that matches the
> completeness and navigability people expect from a serious open-source
> project, while keeping the repo's Obsidian vault as the architecture /
> planning source of truth.

**Scope:** `musubi/` public documentation surface.  
**Primary surfaces:** GitHub Wiki first, repo-owned markdown as source, generated API reference from `openapi.yaml`.  
**Depends on:** the existing vault docs in `docs/Musubi/`, `README.md`, deploy runbooks, and live API assets.

## Why this exists

The repo already has strong raw material:

- [README.md](../../../README.md) is a solid landing page and quick pitch.
- [[07-interfaces/index|Interfaces]], [[08-deployment/index|Deployment]], [[09-operations/index|Operations]], and [[10-security/index|Security]] contain substantial technical content.
- The Obsidian vault is already acting as the architecture and planning source of truth.

What is missing is the **public documentation product**:

- a clear information architecture for users, operators, adapter builders, and contributors
- end-to-end onboarding beyond the current quick start
- task-oriented tutorials and how-to guides
- browsable API / SDK reference with examples
- troubleshooting, FAQ, and maintenance guidance

This note turns "we should document this properly" into an executable backlog.

## Goals

1. A new evaluator can understand what Musubi is, why it exists, and whether it fits their use case within 10 minutes.
2. A self-hosting user can install and run Musubi without needing to reverse-engineer the architecture vault.
3. A developer can find API, SDK, config, and deployment reference material quickly.
4. A contributor can understand how public docs relate to the deeper architectural specs and ADRs.
5. The docs stay maintainable: repo-authored, reviewable in PRs, and grounded in live source assets (`openapi.yaml`, runbooks, config).

## Non-goals

- Replacing the Obsidian vault. The vault remains the design / ADR / planning surface.
- Duplicating every internal note publicly. Some vault content should stay as internal architecture explanation rather than user docs.
- Writing prose directly in the GitHub Wiki UI as the canonical source. That makes review, versioning, and synchronization harder.

## Source-of-truth model

Use the vault as the upstream source for **explanation** and architectural grounding, but treat the public docs as a separate, user-facing layer:

- **Vault (`docs/Musubi/`)**: architecture, ADRs, planning, operator reasoning, design constraints.
- **Public docs source (repo-owned markdown)**: tutorials, installation, configuration, API guides, troubleshooting, contributor-facing explanations.
- **Generated assets**: API reference from [[07-interfaces/openapi/README|OpenAPI snapshots]] / `openapi.yaml`.
- **GitHub Wiki**: published surface, synced from repo-owned docs rather than hand-edited.

## Audiences

### Evaluator

Someone landing from GitHub who wants to know: what is Musubi, what problem does it solve, and why is its memory model different?

### Self-hosting operator

Someone who wants to deploy Musubi on a homelab or dedicated host and needs practical install, config, backup, upgrade, and troubleshooting docs.

### Adapter / SDK developer

Someone integrating against the canonical API or SDK and looking for examples, contract details, and versioning expectations.

### Contributor

Someone changing code or docs who needs to understand the repo conventions, architecture map, and where public docs should live.

## Proposed docs shape

Use a simple **tutorials / how-to / reference / explanation** split so readers can find the right page shape quickly.

### Tutorials

Step-by-step paths for first success:

- Quick Start
- Install Musubi locally
- Capture your first memory
- Run your first retrieval query
- Add thoughts / streaming into an agent workflow

### How-to guides

Task-oriented pages:

- Configure auth / tokens
- Point an adapter at Musubi
- Attach artifacts
- Run lifecycle sweeps manually
- Back up and restore a host
- Upgrade to a new image digest
- Troubleshoot TEI / Ollama / Qdrant issues

### Reference

Exact lookup material:

- API endpoints
- SDK reference
- Config / env vars
- Deployment topology
- Metrics / health endpoints
- Error model
- Namespace model

### Explanation

Conceptual material:

- The three planes
- Lifecycle sweeps
- Agent-as-tenant namespace model
- Why Obsidian is the curated plane
- Security model
- ADR overview

## Target wiki sitemap

### Foundation

- `Home`
- `Quick Start`
- `Installation`
- `Configuration`
- `Core Concepts`

### Learn by doing

- `Tutorial: Capture and Retrieve Your First Memory`
- `Tutorial: Curated Knowledge and the Vault`
- `Tutorial: Build Against the API`

### Task-oriented guides

- `How To: Run Musubi Locally`
- `How To: Deploy on a Single Host`
- `How To: Configure Auth`
- `How To: Ingest Artifacts`
- `How To: Operate Lifecycle Sweeps`
- `How To: Back Up and Restore`
- `How To: Troubleshoot a Broken Deploy`

### Reference

- `API Reference`
- `SDK Guide`
- `Configuration Reference`
- `Deployment Reference`
- `Operations Reference`
- `Security Reference`
- `Glossary`

### Project / contributor context

- `Architecture Overview`
- `ADRs and Design Decisions`
- `Contributing`
- `FAQ`

## Content mapping from existing sources

### Ready to adapt with relatively light rewrite

- [README.md](../../../README.md)
- [[07-interfaces/index]]
- [[08-deployment/index]]
- [[09-operations/index]]
- [[10-security/index]]
- [CONTRIBUTING.md](../../../CONTRIBUTING.md)
- [SECURITY.md](../../../SECURITY.md)
- [deploy/runbooks/first-deploy.md](../../../deploy/runbooks/first-deploy.md)
- [deploy/runbooks/upgrade-image.md](../../../deploy/runbooks/upgrade-image.md)

### Needs translation from architecture-speak to user-docs language

- [[00-index/index]]
- [[01-overview/index]]
- [[03-system-design/index]]
- [[04-data-model/index]]
- [[06-ingestion/index]]
- [[13-decisions/index]]

### Needs new writing

- True end-to-end installation guide
- API quickstart with `curl` examples
- SDK quickstart with minimal Python examples
- Troubleshooting / FAQ pages
- Docs publishing workflow and wiki sync instructions

## Backlog

### D0 — Docs architecture and authoring model

**Intent:** decide how the public docs are authored, reviewed, and published before writing dozens of pages.

- [ ] Decide the repo-owned source path for public docs (`docs/public/`, `docs/wiki/`, or equivalent).
- [ ] Decide whether the GitHub Wiki is a sync target only or also a manually editable surface. Recommendation: sync target only.
- [ ] Document the source-of-truth rule: vault for architecture, public docs for user-facing material, generated API docs from source assets.
- [ ] Define a lightweight docs style guide: audience-first titles, command-copyable examples, minimal internal jargon, explicit prerequisites.
- [ ] Add a docs inventory table mapping current sources to target wiki pages.

**DoD:** one committed note or README describing where docs live, how they publish, and which source owns which class of content.

### D1 — Foundation pages

**Intent:** create the landing and onboarding pages that every serious OSS project needs.

- [ ] `Home`
- [ ] `Quick Start`
- [ ] `Installation`
- [ ] `Configuration`
- [ ] `Core Concepts`

Each page should answer a distinct first-time question:

- `Home` = what Musubi is, why it exists, where to start
- `Quick Start` = shortest path to a working local instance
- `Installation` = supported install paths and prerequisites
- `Configuration` = runtime knobs, secrets, and environment model
- `Core Concepts` = planes, lifecycle, namespaces, vault, adapters

**DoD:** a new reader can get from repo landing page to working local install without opening architecture notes unless they want deeper context.

### D2 — Tutorials

**Intent:** give users concrete success paths instead of only explanation and reference.

- [ ] Tutorial: capture and retrieve a memory
- [ ] Tutorial: create curated knowledge through the vault path
- [ ] Tutorial: call the API directly
- [ ] Tutorial: integrate via the Python SDK

**DoD:** each tutorial runs end-to-end with copyable commands or code and names its prerequisites, expected output, and cleanup.

### D3 — How-to guides

**Intent:** cover common operator and builder tasks with practical steps.

- [ ] Run Musubi locally for development
- [ ] Deploy on a single host
- [ ] Configure auth / tokens
- [ ] Ingest artifacts
- [ ] Run or inspect lifecycle sweeps
- [ ] Back up and restore
- [ ] Upgrade safely
- [ ] Troubleshoot unhealthy services

**DoD:** common operational tasks can be completed from the guide alone, with links out only for deep background.

### D4 — Reference layer

**Intent:** make exact lookup material easy to find and keep it close to live sources.

- [ ] API reference generated from `openapi.yaml`
- [ ] SDK reference and common usage patterns
- [ ] Configuration / env var reference
- [ ] Error / result model reference
- [ ] Deployment topology reference
- [ ] Metrics / health / readiness reference
- [ ] Glossary

**DoD:** the docs have a reference section where readers can answer "what is the exact field / endpoint / variable / metric?" without searching the codebase.

### D5 — Trust, project, and contributor pages

**Intent:** round out the project-level docs surface so the repo reads like a mature open-source project.

- [ ] Contributing page aligned with the current repo workflow
- [ ] Security page aligned with `SECURITY.md`
- [ ] Architecture overview for external readers
- [ ] ADR / design-decision landing page
- [ ] FAQ
- [ ] Release / upgrade notes discoverability

**DoD:** contributors and evaluators can understand project process, trust posture, and architectural intent without diving straight into the full vault.

### D6 — Publishing and maintenance

**Intent:** prevent the docs from drifting after the first push.

- [ ] Add a repeatable publish / sync workflow to the GitHub Wiki.
- [ ] Decide which pages are generated vs hand-authored.
- [ ] Add a periodic docs review checklist tied to releases or milestones.
- [ ] Add link-checking / basic docs validation if the surface grows enough to justify it.
- [ ] Define the maintenance loop for updating public docs when ADRs or user-facing behavior change.

**DoD:** docs updates become part of normal repo work, not a one-time sprint.

## Execution order

Recommended order:

1. **D0** — authoring model and page inventory
2. **D1** — landing + onboarding
3. **D4** — API / config / SDK reference baseline
4. **D3** — operational how-to guides
5. **D2** — polished tutorials
6. **D5** — contributor / trust / FAQ
7. **D6** — automation and maintenance loop

Why this order:

- D0 prevents rework.
- D1 gives the project an immediate public docs spine.
- D4 keeps reference grounded in live source assets early.
- D3 closes the biggest operator gap after the quick start.
- D2 becomes much easier once the foundation and reference layers exist.

## Risks and watch-outs

- **Vault language drift:** the architecture notes are strong, but many pages are written for builders, not first-time users. Expect rewrite, not copy-paste.
- **Source duplication:** if the same concept lives in the vault, README, wiki, and runbooks with no ownership rule, the docs will rot quickly.
- **Generated vs prose confusion:** endpoint and schema reference should be generated or at least source-linked where possible.
- **GitHub Wiki ergonomics:** convenient for readers, weaker for authoring. Keep repo markdown authoritative.

## Success criteria

This backlog counts as successful when:

- A new GitHub visitor can find a clear docs home and install path.
- A self-hosting operator can deploy and troubleshoot without spelunking the vault.
- An integrator can build against Musubi from API / SDK docs and examples.
- Public docs and the vault complement each other rather than duplicating or contradicting each other.

## Related

- [[12-roadmap/next-up]]
- [[12-roadmap/index]]
- [[00-index/reading-tour]]
- [[07-interfaces/index]]
- [[08-deployment/index]]
- [[09-operations/index]]
- [[10-security/index]]
- [[13-decisions/index]]
