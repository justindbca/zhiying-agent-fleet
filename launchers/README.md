# Launchers

Codex/Pi startup `.command` files related to cmux-fleet-pi live here.

- `start-pi-full.command` — start Pi in this project with full local access.
- `start-pi-codex-dangerous-full.command` — open a cmux workspace running Pi with the OpenAI Codex provider.
- `start-codex-flashnext.command` — start local FlashNext through Pi using the `flashnext/...` model.

Projects that use the fleet may keep only thin compatibility wrappers that `exec` these files. Do not put fleet launcher logic back into those projects.
