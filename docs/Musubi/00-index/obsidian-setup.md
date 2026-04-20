---
title: Obsidian Setup & Verification
section: 00-index
type: index
status: complete
tags: [section/index, status/complete, type/index]
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: true
---

# Obsidian Setup & Verification

The vault ships with pre-written configs in `.obsidian/`. This note is the
checklist to verify everything survived restart, plus the gotchas that require
a UI click-through.

## One-time: restart Obsidian

Plugin configs in `.obsidian/plugins/<plugin>/data.json` are re-serialized by
each plugin on load. Any change you (or I) make while Obsidian is running may
be overwritten silently. **Close Obsidian completely (Cmd+Q on macOS) and
reopen the vault** — then the configs are loaded from disk as written.

## Checklist after restart

### Core app settings (Settings → Editor, Settings → Files & links)

- [x] **Files & links → New link format** = `Absolute path in vault` ✅ 2026-04-17
- [x] **Files & links → Use `[[Wikilinks]]`** = ON ✅ 2026-04-17
- [x] **Files & links → Automatically update internal links** = ON ✅ 2026-04-17
- [x] **Files & links → Default location for new notes** = `Same folder as current file` ✅ 2026-04-17
- [x] **Files & links → Default location for new attachments** = `In the folder specified below` → `_attachments` ✅ 2026-04-17
- [x] **Editor → Readable line length** = ON ✅ 2026-04-17
- [x] **Editor → Show line numbers** = ON ✅ 2026-04-17
- [x] **Editor → Properties in document** = Shown (not hidden) ✅ 2026-04-17
- [x] **Editor → Strict line breaks** = OFF ✅ 2026-04-17
- [x] **Appearance → CSS snippets** — `musubi-status-colors` enabled ✅ 2026-04-17

### Core plugins (Settings → Core plugins)

All **ON**:

- [x] File explorer ✅ 2026-04-17
- [x] Global search ✅ 2026-04-17
- [x] Quick switcher ✅ 2026-04-17
- [x] Graph view ✅ 2026-04-17
- [x] Backlinks ✅ 2026-04-17
- [x] Outgoing links ✅ 2026-04-17
- [x] Tag pane ✅ 2026-04-17
- [x] Properties ✅ 2026-04-17
- [x] Page preview ✅ 2026-04-17
- [x] Outline ✅ 2026-04-17
- [x] Canvas ✅ 2026-04-17
- [x] Bookmarks ✅ 2026-04-17
- [x] Templates ✅ 2026-04-17
- [x] Note composer ✅ 2026-04-17
- [x] Command palette ✅ 2026-04-17
- [x] File recovery ✅ 2026-04-17
- [x] Bases ✅ 2026-04-17

All **OFF**:

- [x] Audio recorder ✅ 2026-04-17
- [x] Markdown importer ✅ 2026-04-17
- [x] Publish ✅ 2026-04-17
- [x] Random note ✅ 2026-04-17
- [x] Slides ✅ 2026-04-17
- [x] Web viewer ✅ 2026-04-17
- [x] Workspaces ✅ 2026-04-17
- [x] ZK Prefixer ✅ 2026-04-17

### Community plugins (Settings → Community plugins → Installed)

All **enabled**:

- [x] Breadcrumbs ✅ 2026-04-17
- [x] Dataview ✅ 2026-04-17
- [x] Kanban ✅ 2026-04-17
- [x] Linter ✅ 2026-04-17
- [x] Local REST API ✅ 2026-04-17
- [x] Obsidian Git ✅ 2026-04-17
- [x] Style Settings ✅ 2026-04-17
- [x] Tasks ✅ 2026-04-17
- [x] Templater ✅ 2026-04-17

### Breadcrumbs — extra edge fields

Settings → Breadcrumbs → **Fields**. You should see **all ten** labels:

- [x] `up` ✅ 2026-04-17
- [x] `down` ✅ 2026-04-17
- [x] `same` ✅ 2026-04-17
- [x] `next` ✅ 2026-04-17
- [x] `prev` ✅ 2026-04-17
- [x] `depends-on` ✅ 2026-04-17
- [x] `blocks` ✅ 2026-04-17
- [x] `supersedes` ✅ 2026-04-17
- [x] `superseded-by` ✅ 2026-04-17
- [x] `implements` ✅ 2026-04-17

If any are missing (the plugin sometimes strips custom entries on first load):
click **Add Field** and paste the label exactly. Then Settings → Breadcrumbs →
Implied relations → verify the `depends-on ↔ blocks` and `supersedes ↔ superseded-by`
pairs exist. If not, re-add them (see [[00-index/conventions#Breadcrumbs fields]]).

### Tasks — custom statuses

Settings → Tasks → Review and customize statuses. You should see these **custom** statuses beyond Todo / Done:

- [x] `/` In Progress ✅ 2026-04-17
- [x] `-` Cancelled ✅ 2026-04-17
- [x] `R` Research ✅ 2026-04-17
- [x] `!` Important ✅ 2026-04-17
- [x] `?` Question ✅ 2026-04-17
- [x] `b` Blocked ✅ 2026-04-17
- [x] `B` Bookmark ✅ 2026-04-17

### Templater — folder templates

Settings → Templater → **Folder templates**. Each folder mapped to its template:

- [x] `13-decisions` → `_templates/adr.md` ✅ 2026-04-17
- [x] `03-system-design` → `_templates/spec.md` ✅ 2026-04-17
- [x] `04-data-model` → `_templates/spec.md` ✅ 2026-04-17
- [x] `05-retrieval` → `_templates/spec.md` ✅ 2026-04-17
- [x] `06-ingestion` → `_templates/spec.md` ✅ 2026-04-17
- [x] `07-interfaces` → `_templates/spec.md` ✅ 2026-04-17
- [x] `08-deployment` → `_templates/spec.md` ✅ 2026-04-17
- [x] `09-operations` → `_templates/runbook.md` ✅ 2026-04-17
- [x] `10-security` → `_templates/spec.md` ✅ 2026-04-17
- [x] `11-migration` → `_templates/migration-phase.md` ✅ 2026-04-17
- [x] `_inbox/research` → `_templates/research-question.md` ✅ 2026-04-17

### Dataview

Settings → Dataview:

- [x] **Enable JavaScript queries** = ON ✅ 2026-04-17
- [x] **Enable Inline Dataview JS queries** = ON ✅ 2026-04-17
- [x] **Enable Inline Dataview** = ON ✅ 2026-04-17

### Linter

Settings → Linter → General:

- [x] **Lint on save** = ON ✅ 2026-04-17
- [x] **Folders to ignore** contains `_templates`, `_bases`, `_attachments`, `_inbox`, `_tools` ✅ 2026-04-17

### Graph view

Open the graph (Cmd+G) and check:

- [x] **Color groups** panel shows 14 rules (status/* + type/*). ✅ 2026-04-17
- [x] **Display → Arrows** = ON. ✅ 2026-04-17
- [x] **Filters → Show orphans** = ON (so unlinked notes are visible). ✅ 2026-04-17
- [x] **Filters → Show attachments** = OFF (keeps the graph focused). ✅ 2026-04-17

Open a `status: research-needed` note in the vault — the node should glow red.

### Kanban

Open [[12-roadmap/slice-board]]. It should render as a board with lanes
"Backlog / In progress / In review / Done / Shipped (v1)". If it renders as
plain markdown, the plugin isn't picking up the `kanban-plugin: basic`
frontmatter — close and reopen the file.

### Local REST API

Settings → Local REST API:

- [x] Enable **HTTPS** on port 27124. ✅ 2026-04-17
- [x] Copy the API key into your password manager — you'll need it for ✅ 2026-04-17
      [[_tools/README|slice_watch.py]] integrations and any future scripts.
- [x] Test: `curl -k -H "Authorization: Bearer <key>" https://127.0.0.1:27124/vault/00-index/dashboard.md` ✅ 2026-04-17

## What to do if something drifts again

Plugin `data.json` files can be silently overwritten by the plugin on next
load. Signals to watch for:

- **Breadcrumbs** shows only 5 edge fields instead of 10.
- **Tasks** is missing the `R`/`!`/`?` custom statuses.
- **Graph** color groups dropped to zero.

Recovery:

1. Close Obsidian.
2. Re-copy the reference config — the canonical content lives in this vault's
   git history. `git restore .obsidian/plugins/<name>/data.json`.
3. Reopen Obsidian.
4. Walk the checklist above.

The root cause is documented in the project memory: plugin `data.json` is a
cache of in-memory settings, not a hand-edited source of truth. Set via the UI
when possible; write to file only as a first-time seed.

## Related

- [[00-index/conventions]] — the full convention set.
- [[README]] — vault overview.
- [[_tools/README]] — vault validation tools.
