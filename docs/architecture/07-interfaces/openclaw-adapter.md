---
title: OpenClaw Adapter
section: 07-interfaces
tags: [adapter, browser, interfaces, openclaw, section/interfaces, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-19
up: "[[07-interfaces/index]]"
reviewed: false
implements: "docs/architecture/07-interfaces/"
---
# OpenClaw Adapter

Integrates Musubi into the OpenClaw browser extension. Captures web-browsing context as episodic memories, surfaces retrieval results inline in web pages, and sends/receives thoughts across the user's other presences.

**Implementation lives in a sibling repo:** `github.com/ericmey/openclaw-musubi` (TypeScript browser extension). Per [[13-decisions/0022-extension-ecosystem-naming]] (ADR-0022), non-Python integrations live in external `<system>-musubi` repos so that their toolchain (pnpm, tsc, vitest) and host-system release cadence (Chrome Web Store, Firefox Add-ons) don't mix with Musubi's Python monorepo.

This spec describes the **contract** the extension implements against Musubi's canonical API. The contract lives with Musubi (here); the implementation lives in `openclaw-musubi`. TypeScript types are generated from `openapi.yaml` (in this repo) via `openapi-typescript`, giving the extension compile-time safety against the canonical API without hand-maintaining a parallel type file. Optional Python side-car for heavy lifting (e.g., full-page extraction) is out of initial scope; if needed, it would ship as a future `packages/musubi-openclaw-sidecar/` workspace subpackage in this repo.

## What OpenClaw is

OpenClaw (April 2026): an AI-assisted browser extension that observes the user's page context, suggests actions, and delegates work to agent back-ends. It needs persistent memory that spans the user's browsing, communicates with coding/voice presences, and surfaces relevant context on-page.

## Capabilities

### Capture

- **On-page text selection**: user highlights → "Remember this" → episodic memory with `capture_source: "openclaw-selection"`, `source_ref: <page url>`.
- **Page capture**: "Save this page for later" → full page as an artifact (`content_type: text/html`, `chunker: html-extractor-v1` — uses Mozilla Readability-style extractor).
- **Annotation**: user adds a note pinned to a URL → episodic memory with `source_ref: <url>` + `content` = the note.

### Retrieve

- **Context sidebar**: as the user lands on a page, the adapter queries Musubi (fast path, planes=`[curated, concept]`) for anything relevant to the page's URL, title, or visible text. Renders as a side panel.
- **Inline suggestions**: when the user highlights a term, a tooltip shows 3 related memories / curated notes.
- **Global search**: a keyboard shortcut opens a search bar that queries Musubi (deep path).

### Thoughts

- **Receive**: subscribe to thoughts addressed to `openclaw`; show as an unread badge in the extension popup.
- **Send**: "tell my coding presence to…" button sends a thought to `eric/claude-code`.

## Architecture

```
┌──────────────────────────────┐
│  Browser tab content script  │   (page DOM integration; highlights, tooltips, sidebar)
└──────────────┬───────────────┘
               │ postMessage
               ▼
┌──────────────────────────────┐
│   Service worker (BG)         │   (stateful; holds Musubi client; OAuth)
└──────────────┬───────────────┘
               │ HTTPS
               ▼
┌──────────────────────────────┐
│  Musubi Core (home LAN or    │
│  internet-facing endpoint)   │
└──────────────────────────────┘
```

The service worker holds the OAuth token and connection pool. Content scripts post messages to the service worker; never talk to Musubi directly.

Optional: a local Python side-car for heavy extraction (full-page → cleaned article text), exposed over `localhost:xxxxx` via a tiny HTTP server. Off by default; enable for power users.

## Presence mapping

- OpenClaw → `eric/openclaw`
- Captures under `eric/openclaw/episodic` by default.
- Shared curated via `eric/_shared/curated`.
- Listens for thoughts addressed to `openclaw` or `all`.

## Page-relevance retrieval

When a page loads:

```typescript
const query = buildPageContextQuery(page);
const results = await musubi.retrieve({
  namespace: "eric/_shared/blended",
  query_text: query,
  mode: "fast",
  limit: 5,
  planes: ["curated", "concept"],
  filters: {
    topics_any: extractTopicsFromUrl(page.url),
  },
});
renderSidebar(results);
```

`buildPageContextQuery(page)`:

- If page has an `<article>`, use its first 500 chars.
- Else, page title + meta description + first 3 headings.

Topics from URL:

- `github.com/livekit/agents` → `[infrastructure/livekit, projects/livekit]`
- `docs.qdrant.io/concepts` → `[infrastructure/qdrant]`

Heuristic, improves with user feedback. Fallback: no topic filter.

## Artifact upload flow

```typescript
async function savePage() {
  const html = await extractCleanedPage();
  const blob = new Blob([html], {type: "text/html"});
  await musubi.artifacts.upload({
    namespace: "eric/openclaw/artifact",
    title: document.title,
    content_type: "text/html",
    source_system: "openclaw",
    source_ref: document.location.href,
    file: blob,
  });
  showToast("Saved to Musubi");
}
```

Extractor uses Mozilla Readability (open-source). Images are referenced but not bundled — the extractor keeps `<img src>` tags for later resolution.

## Offline behavior

When offline:

- Captures queue locally (IndexedDB).
- Retrieval queries fail with a friendly error ("Musubi unreachable").
- Thought checks pause.

When back online:

- Queue drains to Musubi via the capture API.
- Thought polling resumes.

Queue has a 1000-entry cap; oldest dropped with a warning.

## Auth

OAuth 2.1 with PKCE. Flow:

1. First use: extension opens a browser tab to Musubi's OAuth endpoint.
2. User logs in, approves namespace scope.
3. Auth endpoint redirects to `chrome-extension://<id>/oauth/callback` with a code.
4. Extension exchanges code + PKCE verifier for a token.
5. Token stored in `chrome.storage.local` (encrypted at rest by Chromium).
6. Refresh tokens rotated per OAuth 2.1 spec.

Token revocation: user clicks "Sign out" in the extension; revoke endpoint called.

## Permissions model

Each captured memory's `namespace` must be within the token's scope. The adapter never sends to `eric/_shared/curated` directly — that's reserved for humans in Obsidian or for Lifecycle Worker promotions.

## Observability

- `openclaw.capture.count{source: selection|page|note}` counter.
- `openclaw.retrieve.latency_ms` histogram.
- `openclaw.thought.received` counter.
- `openclaw.offline_queued` counter.

Metrics forwarded to a small local collector; not public.

## UX rules

- **Never auto-capture.** Every capture is explicit (user clicks or confirms).
- **Auto-retrieve is opt-in.** Sidebar retrieval is off by default; user enables per-site.
- **Privacy first.** No capture on pages marked private, banking domains, or user-configured excludes.
- **Export**: "Export all my memories" — downloads JSON + Markdown of everything the extension captured. User always owns the data.

## Test Contract

**Module under test:** `musubi-openclaw-adapter/src/*` (TypeScript)

Capture:

1. `test_selection_capture_sends_expected_payload`
2. `test_page_capture_extracts_with_readability`
3. `test_page_capture_uploads_as_artifact`
4. `test_note_capture_captures_with_source_ref`

Retrieve:

5. `test_page_context_query_built_from_article`
6. `test_page_context_query_falls_back_to_title_and_headings`
7. `test_topics_extracted_from_url_patterns`
8. `test_sidebar_renders_top_5_results`

Thoughts:

9. `test_thought_check_runs_on_popup_open`
10. `test_thought_badge_updates_on_new_thought`
11. `test_thought_send_button_invokes_sdk`

Offline:

12. `test_offline_capture_queues_to_indexeddb`
13. `test_online_drain_processes_queue_in_order`
14. `test_offline_queue_caps_at_1000`

Auth:

15. `test_oauth_pkce_flow_completes`
16. `test_token_refreshed_before_expiry`
17. `test_sign_out_revokes_token`

Privacy:

18. `test_excluded_domains_not_captured`
19. `test_auto_retrieve_off_by_default`
20. `test_export_produces_full_json_and_markdown`

Integration:

21. `integration: end-to-end — capture a selection, verify it surfaces in Musubi retrieval`
22. `integration: canonical contract suite via adapter`
