---
title: "`_tools/`: docs health check"
section: _tools
type: index
status: complete
tags: [type/index, status/complete, tooling]
updated: 2026-10-01
up: "[[00-index/index]]"
reviewed: true
---

# `_tools/`: docs health check

`check.py` is a standalone script that validates the architecture docs. It needs only the standard
library (PyYAML is used if present). CI runs `check.py all` on every PR that touches `docs/Musubi/`;
locally, `make agent-check` runs the same thing.

```bash
python3 docs/Musubi/_tools/check.py all          # everything; exits nonzero on any error
python3 docs/Musubi/_tools/check.py vault        # frontmatter fields, H1/title match, tag namespaces
python3 docs/Musubi/_tools/check.py specs        # Test Contract presence, implements: field
python3 docs/Musubi/_tools/check.py wikilinks    # every [[link]] resolves (.md, .canvas, .base)
python3 docs/Musubi/_tools/check.py all --json   # machine-readable
```

| Check | Severity | Rule |
|---|---|---|
| Missing frontmatter field | error | `title`, `section`, `type`, `status`, `tags`, `updated` required |
| Broken wikilink | error | the target note doesn't exist (code spans and `_templates/` are skipped) |
| Section field mismatch | warning | `section:` must equal the parent folder |
| H1 ≠ title | warning | the visible H1 should match the frontmatter `title` |
| Missing tag namespace | warning | every note needs `status/*` and `type/*` tags |
| Complete spec with no Test Contract | warning | spec marked complete, but no `Test Contract` section |
| Complete spec without `implements:` | warning | spec marked complete, but no code-path pointer |

Errors (`✗`) fail CI; warnings (`⚠`) don't. The slice checks and the `slice_watch.py` notifier were
retired with the slice workflow on 2026-09-30.

## Related

- [[00-index/definition-of-done]]: merge gate checklist.
