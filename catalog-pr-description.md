# PR description: catalog entry `ether-btc/turn-completion-nudge`

## What this PR adds

One file: `plugin-catalog/turn-completion-nudge.yaml` — a NousResearch/hermes-agent plugin catalog entry for `ether-btc/turn-completion-nudge`, pinned to commit `6b81c43ce1a1f5dc6c5878d96e4bb86be83fc4ab` (version 0.3.0), `subdir: plugin`. Entry shape mirrors `plugin-catalog/ram-first-context.yaml` (fields: name, repo, sha, subdir, description, maintainer, tier, category, requires_hermes, version, known_issues, capabilities).

## What the plugin does

turn-completion-nudge is a standalone Hermes plugin (`kind: standalone`, `requires_hermes: >=0.21.3`) that scores each completed turn and, when the turn looks incomplete (the agent was planning or announcing next steps it never did), injects a one-off continuation nudge via the `post_llm_call` hook. Fail-open: scoring or network problems never block a turn, and the hook returns None so the turn continues.

Scorer lane ladder (two hops max): (1) OpenRouter decisions route at `scorer_model` using `OPENROUTER_API_KEY` (declared `requires_env`, read only from that env var — never `.env`); (2) on missing key / HTTP failure / timeout, one keyless Amos systemone fallback hop (`api.amos.scira.ai/v1/systemone`, model `medium`, server may serve a smaller model — the served id is logged). Per-hop timeout 4 s. Metrics buckets `lane_openrouter` / `lane_amos` / `lane_failed` / `dormant`.

Status command: `hermes turn-completion-nudge status` (enabled, opted-in, threshold, last 5 scores, bucket counts — process-local).

## Hermes surfaces used

- `post_llm_call` hook (declared in `capabilities.provides_hooks`, matches `plugin.yaml provides_hooks`; validator-enforced at `hermes plugins validate`)
- `ctx.get_config` read live per call (`enabled`, `send_turn_text`, `threshold`, `timeout_s`, `scorer_model`, `amos_model`)
- `requires_env: [OPENROUTER_API_KEY]`; `optional_env: [HERMES_AMOS_BASE_URL]`
- `ctx.register_cli_command` for `hermes turn-completion-nudge status`
- Pure stdlib (`urllib`, `json`, `threading`); no third-party dependencies

## Pin

- repo: https://github.com/ether-btc/turn-completion-nudge (now live; pushed 2026-11-09)
- sha: 6b81c43ce1a1f5dc6c5878d96e4bb86be83fc4ab (full 40-hex; the repo is live at the pinned URL, default branch `main` = `48c810b` — one owner-approved bookkeeping commit ahead of the pin, adding only `docs/MISSION-review-fixwave-2.md` closeout markers; convention documented in the repo)
- subdir: plugin
- version: 0.3.0 (matches the pinned code)

## Tree state since the independent review (GO verdict, three conditionals — all closed)

The entry (`66cabd7..48c810b`) landed a post-review fix wave (WP-r2):

- `3e65a4b` privacy-first dormant-by-default when `send_turn_text` is absent (production semantics).
- `1132588` repo layout aligned to the house standard (`plugin/` subdirectory).
- `2c5e660` pytest-harness bridge for the dormant default.
- `55dc51f` review wave: shared strict bool parser `_parse_strict_bool` wired into the privacy guard AND `_read_lane_config` (string `"false"` no longer enables egress); production-default test added (`PYTEST_CURRENT_TEST` cleared → absent key → dormant); duplicate `pi5_evidence.json` deduped; catalog deliverables committed.
- `6b81c43` contradictory empty `provides_commands: []` dropped (house-standard field set; `hermes turn-completion-nudge status` stays README+source-declared).
- `14b1427`/`48c810b` SHA re-pin + bookkeeping (pin convention: **latest commit whose diff touches `plugin/` content**; HEAD one bookkeeping commit ahead is expected and documented in the repo).

Verification at the pinned commit: `hermes plugins validate` 15/15 checks pass; `pytest` **56 passed**; independent senior review verdict GO (its three conditional fixes are the WP-r2 wave above).

## Rule 13 disclosures (risky behaviour)

- **Network calls to third-party services:** yes — OpenRouter decisions API when `OPENROUTER_API_KEY` is set and scoring is active; keyless Amos systemone fallback (`api.amos.scira.ai/v1/systemone`) on lane miss / HTTP failure / timeout. Amos egress happens even without an API key.
- **Clip + send:** each scoring call sends exactly one clipped payload (~1200-char user request + ~1500-char assistant message) to the scorer endpoint; vendors may retain requests.
- **Egress:** production default is dormant when `send_turn_text` is absent (plugin-enforced), but Hermes does not apply `config_schema` defaults (`get_config` returns raw settings only) — an explicit `send_turn_text: true` still sends clipped turn text via the keyless Amos fallback (one POST per scored turn). Opt-in egress remains possible. Under pytest (`PYTEST_CURRENT_TEST` set) the plugin opts tests in (harness bridge).
- **Reads outside the plugin's own data:** turn text read via Hermes ctx at scoring time only when the lane fires, clipped to ~1200/~1500 chars.
- **Shell commands:** none. No subprocess use.
- **Long-running background processes:** none. Bounded in-process guard (cap 1024, drop-oldest); counters cleared on restart.
- **Stored credentials:** none. `OPENROUTER_API_KEY` read only from the declared env var; never from `.env`.
- **Telemetry / usage reporting:** none. Scoring occurs only when the lane fires; nothing is phoned home about installs or usage.

## Known issues (also in the catalog entry)

1. `scorer_model` default `respan/span-01-lite-20260925:free` may be delisted upstream (model rot) — pin or monitor.
2. Egress remains possible with explicit `send_turn_text: true` (keyless Amos fallback); the dormant-by-default default is plugin-enforced, not Hermes-enforced, since Hermes does not apply config_schema defaults.

## Review status (read before opening)

**WP-6 review (NO-GO for public push) covered commit `66cabd7` only** (tree digest 2c0b657af2f4aef82bead66a9548196237b45950ac46011daeb9fe7253fad71a, start==end): egress blocker reproduced at the default install; lane ladder, hygiene cap, zero-writes, capability declarations, and F1/F2 fixes verified clean — 54/54 tests. WP-6 standing rule: fresh review required after ANY source/test/README/plugin.yaml edit.

The pinned commit `55dc51f5...` POST-DATES that review (source edit: `_send_turn_text_opted_out`). The catalog entry is drafted, but the upstream PR must NOT be opened until a fresh review of `66cabd7..2c5e660` is done and the WP-6 gate is re-evaluated.

## Operator gate

This draft is local only (`completion_contract: local-only`). The plugin repo has no remote configured and no upstream PR exists. Opening the PR against `NousResearch/hermes-agent` `plugin-catalog/` is a hard operator gate — wait for explicit go before pushing or opening anything. CI at admission clones the repo at the pinned sha and runs `hermes plugins validate`; fix failures before the PR is opened. Catalog admission rules require owner-or-major-contributor submission (this repo is owned by `ether-btc`), exact 40-hex sha pin, no self-updating code, and declared capabilities matching registrations.

## Review references

- WP-6 review report: attachment on kanban parent task t_0565ebfd (verdict NO-GO, blocker = default install egresses via keyless Amos fallback; root cause = Hermes get_config returns raw settings, config_schema defaults never applied).
- Entry schema mirrors `plugin-catalog/ram-first-context.yaml` field shape; catalog README entry schema and admission rules 1–16 apply.

## Filed

- Submitted: PR https://github.com/NousResearch/hermes-agent/pull/151924 (2026-11-09), branch `add-turn-completion-nudge-catalog-entry`, one file `plugin-catalog/turn-completion-nudge.yaml`, house-shape (no pin_note; the pin-bookkeeping convention lives above and in the plugin repo README-poetry of evidence).
- Plugin repo live: https://github.com/ether-btc/turn-completion-nudge (main = 48c810b).
