# Security Policy

## Supported versions

Security-relevant fixes land on the latest release line and are published as the next patch release. Older releases are not backported; upgrade to the latest release to receive fixes.

| Version                         | Supported          |
|---------------------------------|--------------------|
| Latest release (see [Releases](https://github.com/sourceblender/musubi/releases)) | ✅ Yes              |
| Any older release               | ❌ No               |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security reports.**

Use one of these paths instead:

1. **GitHub Private Vulnerability Reporting** — preferred. Go to the [Security tab](https://github.com/sourceblender/musubi/security/advisories/new) on this repo and open a draft advisory. It's routed directly to the maintainer and isn't publicly indexed until published.
2. **Email** — `eric@sourceblender.com` with subject prefix `[musubi-security]`.

Please include:

- A description of the issue and why you think it's a vulnerability.
- Reproduction steps (a minimal PoC is ideal).
- The affected version / commit SHA / image digest.
- Your assessment of the impact (data exposure, auth bypass, RCE, DoS, etc.).
- If you'd like credit in the advisory, the name/handle to use.

## Response timeline

Musubi has a single maintainer, so response is best-effort rather than SLA-backed. That said:

- **Acknowledgement:** within 72 hours of report.
- **Initial assessment** (is this reproducible? severity?): within 7 days.
- **Fix or mitigation timeline:** shared with you once the assessment is done — typically days for low severity, a week or two for moderate, as fast as possible for critical.
- **Public disclosure:** coordinated. We prefer to release a fix and a CVE-linked advisory at the same time, giving operators a version to upgrade to before the details are public.

## What counts

### In scope

- The Musubi Core service (the Python code under `src/musubi/`, the published container image at `ghcr.io/sourceblender/musubi-core`, and the published SDK).
- The public Compose files (`docker-compose.yml` and `deploy/docker/`), to the extent they configure Musubi itself.
- The HTTP API surface.
- Auth / session / token handling.
- Anything that affects data integrity across the episodic, concept, curated, or thoughts planes.

### Out of scope

- Third-party dependencies with their own security policies (Qdrant, TEI, Ollama, FastAPI). Report those upstream; we'll pick up their fixes when they ship.
- Denial-of-service against a single-node deployment by an authenticated caller — Musubi has no admission-control hardening for that use case today, and making it robust is on the roadmap rather than a bug.
- Deployment-specific topology disclosed in historical `refs/pull/*` refs from before the repo went public. The current default branch is scrubbed; see commit [`cbcca0b`](https://github.com/sourceblender/musubi/commit/cbcca0b) for details.

## Supply-chain verification

Every published image is signed by a GitHub Actions OIDC identity via [cosign](https://github.com/sigstore/cosign), attested with a CycloneDX SBOM, and scanned by Trivy for CRITICAL CVEs before publication. To verify a given digest before running it:

```bash
cosign verify \
  --certificate-identity 'https://github.com/sourceblender/musubi/.github/workflows/publish-core-image.yml@refs/tags/<version>' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/sourceblender/musubi-core@sha256:<digest>
```

The attestation can be retrieved with:

```bash
cosign download attestation \
  --predicate-type 'https://cyclonedx.org/bom' \
  ghcr.io/sourceblender/musubi-core@sha256:<digest>
```

## No bounties

There is no bug bounty program, no payouts, no swag. What we can offer in return for a responsible report is recognition in the advisory and a sincere thank-you.
