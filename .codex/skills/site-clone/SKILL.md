---
name: site-clone
description: "Mirror a full website with HTTrack first, then use Playwright-driven extraction to rebuild it with auditable blueprints and maintainable code."
argument-hint: "<url1> [<url2> ...]"
user-invocable: true
---

# Site Clone (HTTrack + Playwright Edition)

You are about to upgrade the original `site-clone` workflow into a two-track cloning system for **$ARGUMENTS**.

The workflow is strict:

1. **Backup first with HTTrack**
2. **Extract live behavior with Playwright MCP**
3. **Build from blueprints, not guesses**

This skill adopts:

- `HTTrack` for complete offline mirror coverage
- `Playwright MCP` for real browser interaction capture
- `Perfect Web Clone` style blueprinting and verification patterns

It does not embed the full upstream `perfect-web-clone` application. Instead, it reuses the parts that fit Codex best: mirror-first capture, topology mapping, section blueprints, and explicit behavior extraction.

## Mandatory Tooling

- `HTTrack` must be installed locally
- `Playwright MCP` must be available for browser control

If `httrack` is missing, stop and tell the user to install it. On macOS, recommend:

```bash
brew install httrack
```

If `Playwright MCP` is missing, stop and tell the user to add:

```text
npx @playwright/mcp@latest
```

## Workflow Overview

### Stage 0: Create the Full Offline Mirror

For each target URL:

1. Run:

```bash
./scripts/prepare-clone-target.sh <url>
```

2. Confirm these outputs exist:
   - `captures/<host>/<timestamp>/mirror/`
   - `captures/<host>/<timestamp>/research/mirror-manifest.json`
   - `captures/<host>/<timestamp>/research/target.json`

3. Treat the mirror as the source of truth for:
   - reachable page inventory
   - static assets
   - rewritten local links and hotspots
   - offline validation

Do not skip this stage. The mirror is the forensic backup.

### Stage 1: Live Browser Survey

After the mirror succeeds, use Playwright MCP against the live site.

Capture:

- full-page screenshots at desktop and mobile widths
- page topology
- responsive breakpoints
- hover states
- click-driven states
- scroll-triggered changes
- modals, tabs, carousels, accordions, and forms

Save findings under the corresponding `captures/<host>/<timestamp>/research/` directory.

### Stage 2: Blueprint the Page

Before building any component, create:

- `TOPOLOGY.md`
- `BEHAVIORS.md`
- component blueprint files in `research/components/`

Every blueprint must include:

- target component file
- screenshot reference
- interaction model
- exact computed styles
- state transitions
- asset dependencies
- implementation notes

### Stage 3: Build Against Both Sources

When generating code:

- use the live site for interaction truth
- use the HTTrack mirror for complete content and asset validation

If the live site and mirror disagree:

1. prefer the live site for runtime behavior
2. prefer the mirror for missing asset recovery and crawl coverage
3. document the discrepancy

### Stage 4: Verify

Verification is required:

- local build passes
- mirrored pages open offline
- generated implementation matches live site behavior
- no missing mirrored assets remain unresolved

## Core Principles

### 1. Mirror Before Interpretation

Do not start with screenshots alone. Get the offline mirror first so the page cannot drift underneath the workflow.

### 2. Use Third-Party Tools Directly

Do not reimplement mirroring logic. Use `HTTrack` for crawling and link rewriting.

### 3. Extract Behavior Explicitly

Hover, scroll, tab, and modal behavior must be measured from the real page with Playwright MCP.

### 4. Build From Contracts

Blueprints are contracts between extraction and implementation. If a builder would need to guess, the blueprint is incomplete.

### 5. Verify From Two Angles

Every serious clone should be checked against:

- the live page for behavior
- the offline mirror for completeness

## Recommended Commands

Prepare a target:

```bash
./scripts/prepare-clone-target.sh https://example.com
```

Regenerate a manifest later:

```bash
node ./scripts/generate-mirror-manifest.mjs ./captures/example.com/latest/mirror
```

## Upstream References

- HTTrack: <https://github.com/xroche/httrack>
- Perfect Web Clone: <https://github.com/ericshang98/perfect-web-clone>
