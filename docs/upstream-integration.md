# Upstream Integration Notes

This repository upgrades the existing `site-clone` skill by combining two upstream approaches instead of replacing one with the other.

## HTTrack Adoption

Adopted directly:

- full-site recursive mirroring
- offline link rewriting
- static asset preservation
- mirror-first backup workflow

Why:

- `HTTrack` is still the strongest low-friction option for preserving the complete static footprint of a site.
- It gives the cloning pipeline an auditable snapshot even if the target site changes later.

## Perfect Web Clone Adoption

Adopted as workflow patterns, not as an embedded runtime:

- browser-first extraction after backup
- section topology mapping
- component blueprint files
- interaction-state capture
- verification after each build phase

Why not embed the whole upstream app:

- the upstream project is a full standalone product with its own backend, frontend, sandbox, and agent runtime
- embedding all of it would make the skill heavier without improving the local Codex workflow proportionally
- the highest-value part for this repository is the extraction methodology, not the entire application shell

## Result

The upgraded skill now follows this order:

1. run `HTTrack` for complete offline backup
2. generate a mirror manifest for auditability
3. use `Playwright` for live interaction and responsive extraction
4. build section blueprints
5. implement and verify against both live site and offline mirror
