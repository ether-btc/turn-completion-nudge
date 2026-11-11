# PR description: catalog entry `ether-btc/turn-completion-nudge`

## What this PR adds

One file: `plugin-catalog/turn-completion-nudge.yaml` — a NousResearch/hermes-agent plugin catalog entry for `ether-btc/turn-completion-nudge`, pinned to commit `3453efe28689f3855eb9865e9033f8823a0961e2` (version 0.3.2), `subdir: plugin`. Entry shape mirrors `plugin-catalog/ram-first-context.yaml` (fields: name, repo, sha, subdir, description, maintainer, tier, category, requires_hermes, version, known_issues, capabilities).

## What the plugin does

turn-completion-nudge is a standalone Hermes plugin (`kind: standalone`, `requires_hermes: >=0.21.3`) that scores each completed turn and, when the turn looks incomplete (the agent was planning or announcing next steps it never did), injects a one-off continuation nudge via the `post_llm_call` hook. Fail-open: scoring or network problems never block a turn, and the hook returns None so the turn continues. Nudge chains are capped at 2 consecutive nudge-driven turns (the injected text is recognized as the next request).

Scorer lane ladder: (1) OpenRouter decisions route at `scorer_model` using `OPENROUTER_API_KEY` (declared `requires_env`, `secret: true`; read via the Hermes secret scope when available, else the env var — never `.env`); (2) the keyless Amos systemone fallback (`api.amos.scira.ai/v1/systemone`, model `medium`) is a **second opt-in** (`amos_fallback`, default false) — an OpenRouter failure without it records `lane_failed` and stops, no second third-party egress. Per-hop timeout 4 s. Metrics buckets `lane_openrouter` / `lane_amos` / `lane_failed` / `dormant`.

Status command: `hermes turn-completion-nudge status` (enabled, opted-in, threshold, last 5 scores, bucket counts — process-local).

## Hermes surfaces used

`post_llm_call`, `capabilities.provides_hooks` / `plugin.yaml provides_hooks`, `hermes plugins validate`, `ctx.get_config` (live per call: `enabled`, `send_turn_text`, `threshold`, `timeout_s`, `scorer_model`, `amos_model`, `amos_fallback`, `amos_base_url`), `requires_env: [OPENROUTER_API_KEY]` with `secret: true`, `agent.secret_scope.get_secret` (preferred key source, env fallback), `ctx.register_cli_command`, `urllib`, `json`, `threading`.

## Pin

- sha: `3453efe28689f3855eb9865e9033f8823a0961e2` (full 40-hex; the repo is live at the pinned URL; default branch `main` carries only owner-bookkeeping commits after the pin — `git diff 3453efe2..main -- plugin/` is empty by convention, and the convention is documented in the repo)

## Tree state since the independent review (GO verdict, three conditionals — all closed)

Landed since the WP-6 review, all below or at the pin plus owner bookkeeping:

- `3e65a4b`/`1132588` post-review fixes around `send_turn_text` (blocker `2c5e660` fixed), then `2c5e660` itself
- `55dc51f` WP-r2 fix wave: strict-bool parser `_parse_strict_bool` shared by `_read_lane_config` and the privacy guard (`"false"` string can no longer enable egress); production-default dormant test; `pi5_evidence.json` deduped
- `6b81c43` `provides_commands: []` dropped (consistent with the `hermes turn-completion-nudge status` CLI wiring)
- `14b1427`/`48c810b`/`6829483` SHA re-pin, catalog-yaml sync to this PR's filed shape, CI-workflow fix (setup-python + pytest install), and PR-description sync — all owner bookkeeping
- **`fc2f527` upstream-review round (2026-11-10, addressing teknium1's CHANGES_REQUESTED):** OpenRouter key read via `agent.secret_scope.get_secret` with env fallback (blocking item); `amos_fallback` second opt-in (default false — no silent second service); consecutive-nudge cap (2) with chain reset on a real user turn or a complete turn; `PYTEST_CURRENT_TEST` branch removed entirely — one default path (absent → dormant everywhere), config passed explicitly in tests; `HERMES_AMOS_BASE_URL` moved from `optional_env` to `config_schema` (`amos_base_url`); version 0.3.1

- **`3453efe2` upstream-review round 2 (2026-11-11, addressing teknium1's second CHANGES_REQUESTED):** `_read_openrouter_key` returns `get_secret(...)` exactly as given — bare `except Exception` and empty-value `os.environ` fallbacks removed (they re-opened the multi-profile case where the launch profile's key gets used and billed); only an `ImportError` fallback remains for Hermes builds without `agent.secret_scope`; tests rewritten to the as-is contract (scope refusal → None even with env set; scope errors propagate, not swallowed); version 0.3.2; live install re-synced byte-identical

Verification at the pinned commit: `hermes plugins validate` 15/15 checks pass; `pytest` **65 passed**; independent senior review verdict GO (its three conditional fixes are the WP-r2 wave above).

## Rule 13 disclosures (risky behaviour)

- Opt-in egress: with `send_turn_text: true` the plugin sends ONLY a clipped payload (~1200-char user request + ~1500-char assistant message) to the OpenRouter decisions endpoint (`OPENROUTER_API_KEY`; vendors may retain requests). The keyless Amos/Scira hop (`api.amos.scira.ai/v1/systemone`) additionally requires `amos_fallback: true` — two separate opt-ins, no silent second service.
- The key is read via the Hermes secret scope (`agent.secret_scope.get_secret`) when importable, falling back to the `OPENROUTER_API_KEY` env var — never `.env`.
- No file writes outside the plugin directory; stdlib only; fail-open everywhere (turn never blocked).
- Dormant by default in every context (no `PYTEST_CURRENT_TEST` sniffing — removed at upstream's request).

## Known issues (also in the catalog entry)

- Free-model rot: default scorer model `respan/span-01-lite-20260925:free` may be delisted upstream — pin or monitor.
- Privacy default: absent `send_turn_text` → dormant; explicit true egresses clipped turn text to the OpenRouter lane (Amos/Scira only with its own opt-in).
- Nudge chains capped at 2 consecutive nudge-driven turns; the hook stays silent until a real user turn arrives.

## Review status

- WP-6 review (NO-GO at the time) covered `66cabd7` only: it reproduced a default-install egress blocker (Hermes `get_config` returns raw settings; `config_schema` defaults are never applied, so an absent `send_turn_text` was treated as opted-in).
- Fresh independent review (2026-11-09, GO verdict) over the full delta plus plugin.yaml/README/digest surfaces: **GO conditional on three fixes** — strict-bool parsing in the privacy guard, a production-default dormant test, and residue/manifest consistency. All three closed by the WP-r2 wave (`55dc51f`..`6b81c43`) and re-verified from the primary artifacts.
- Upstream review (teknium1, 2026-11-10, CHANGES_REQUESTED at `6b81c43`): one blocking item (secret-scope key read) and four recommended items (amos fallback opt-in, consecutive-nudge cap, drop the pytest bridge, env → config for the Amos endpoint). **All five addressed in `fc2f527` (v0.3.1)** — including the recommended set — re-verified: pytest 64/64, `hermes plugins validate` 15/15, live install byte-identical to the pin.

## Operator gate

Closed: the plugin repo is live at https://github.com/ether-btc/turn-completion-nudge (pushed 2026-11-09, operator-authorized) and this catalog PR is open. Remaining external gates are upstream's: maintainer re-review of this delta, catalog admission CI (clones the pinned sha and runs `hermes plugins validate`), and merge — all belong to the maintainers.

## Review references

- WP-6 review report: attachment on kanban parent task t_0565ebfd (verdict NO-GO at `66cabd7`, blocker = default-install egress via the keyless Amos fallback).
- Fresh GO review: conditional verdict over `66cabd7..2c5e660` + surfaces; its three conditional fixes are the WP-r2 wave (`55dc51f`), each re-verified post-fix.
- Upstream review: teknium1, CHANGES_REQUESTED at `6b81c43` (2026-11-10) — addressed in `fc2f527`.
- Entry schema mirrors `plugin-catalog/ram-first-context.yaml` field shape; catalog README entry schema and admission rules 1–16 apply.
