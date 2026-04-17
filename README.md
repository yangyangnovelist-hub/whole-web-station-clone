# Whole Web Station Clone

`Whole Web Station Clone` upgrades the existing `site-clone` workflow into a two-track cloning pipeline:

1. `HTTrack` creates a high-fidelity offline mirror of the full site structure, links, and static resources.
2. `Playwright`-driven extraction handles live DOM state, responsive layouts, hover states, animations, and dynamic interactions.

This repository is intentionally lightweight. It does not vendor the entire upstream `perfect-web-clone` application. Instead, it integrates the parts that fit a Codex skill workflow:

- mirror-first capture
- blueprint-driven extraction
- behavior manifests
- multi-step verification

## Upstream Sources

- `HTTrack`: <https://github.com/xroche/httrack>
- `Perfect Web Clone`: <https://github.com/ericshang98/perfect-web-clone>

## Repository Layout

- `.codex/skills/site-clone/SKILL.md`: upgraded skill definition
- `scripts/run-httrack-mirror.sh`: mirror a target site with `HTTrack`
- `scripts/prepare-clone-target.sh`: create a timestamped capture workspace and generate manifests
- `scripts/generate-mirror-manifest.mjs`: summarize downloaded files and link coverage
- `docs/upstream-integration.md`: what was adopted from upstream and why

## Prerequisites

- macOS or Linux
- `httrack`
- `node` 18+
- Playwright MCP configured in Codex for browser automation

On macOS, install `httrack` with:

```bash
brew install httrack
```

## Basic Usage

Create a mirror workspace:

```bash
./scripts/prepare-clone-target.sh https://example.com
```

Run the bare mirror wrapper directly:

```bash
./scripts/run-httrack-mirror.sh https://example.com ./captures/example.com/latest/mirror
```

Generate or refresh the manifest for an existing mirror:

```bash
node ./scripts/generate-mirror-manifest.mjs ./captures/example.com/latest/mirror
```

## Integration Strategy

The upgraded skill uses `HTTrack` first so the clone process always has a complete offline backup of:

- reachable pages
- downloaded CSS, JS, fonts, and media
- rewritten local links for hotspot validation

It then uses `Playwright` to capture what `HTTrack` cannot fully preserve on its own:

- hover and focus states
- responsive breakpoints
- scroll-triggered transitions
- tab, modal, carousel, and accordion behavior
- client-side hydration differences

This gives you both a forensic mirror and a maintainable implementation workflow.
