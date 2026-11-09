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
- sha: 6b81c43ce1a1f5dc6c5878d96e4bb86be83fc4ab (full 40-hex; the repo is live at the pinned URL; default branch `main` carries only owner-bookkeeping commits after the pin — `git diff 6b81c43..main -- plugin/` is empty by convention, and the convention is documented in the repo)
- subdir: plugin
- version: 0.3.0 (matches the pinned code)

## Tree state since the independent review (GO verdict, three conditionals — all closed)

Landed since the WP-6 review, all below or at the pin plus owner bookkeeping:

- `3e65a4b` privacy-first dormant-by-default when `send_turn_text` is absent (production semantics).
- `1132588` repo layout aligned to the house standard (`plugin/` subdirectory).
- `2c5e660` pytest-harness bridge for the dormant default.
- `55dc51f` review wave: shared strict bool parser `_parse_strict_bool` wired into the privacy guard AND `_read_lane_config` (string `"false"` no longer enables egress); production-default test added (`PYTEST_CURRENT_TEST` cleared → absent key → dormant); duplicate `pi5_evidence.json` deduped; catalog deliverables committed.
- `6b81c43` contradictory empty `provides_commands: []` dropped (house-standard field set; `hermes turn-completion-nudge status` stays README+source-declared).
- `14b1427`/`48c810b`/`6829483`+ SHA re-pin, catalog-yaml sync to this PR's filed shape, CI-workflow fix (setup-python + pytest install), and PR-description sync — all owner bookkeeping (pin convention: **latest commit whose diff touches `plugin/` content**; `plugin/` is byte-identical at pin and at `main`).

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

## Review status

- WP-6 review (NO-GO at the time) covered `66cabd7` only: it reproduced a default-install egress blocker (Hermes `get_config` returns raw settings; `config_schema` defaults are never applied, so an absent `send_turn_text` was treated as opted-in).
- Fresh independent review (2026-11-09, GO verdict) over the full delta plus plugin.yaml/README/digest surfaces: **GO conditional on three fixes** — strict-bool parsing in the privacy guard, a production-default dormant test, and residue/manifest consistency.
- All three conditionals closed by the WP-r2 wave (`55dc51f`..`6b81c43`) and re-verified from the primary artifacts: pytest **56/56**, `hermes plugins validate` **15/15**, live install byte-identical, `plugin/` byte-identical at HEAD and at the pin.
- This PR was opened only after that GO re-review, satisfying the WP-6 standing rule (fresh review after any source/test/README/plugin.yaml edit).

## Operator gate

Closed: the plugin repo is live at https://github.com/ether-btc/turn-completion-nudge (pushed 2026-11-09, operator-authorized) and this catalog PR is open. Remaining external gates are upstream's: maintainer review, catalog admission CI (clones the pinned sha and runs `hermes plugins validate`), and merge — all belong to the maintainers.

## Review references

- WP-6 review report: attachment on kanban parent task t_0565ebfd (verdict NO-GO at `66cabd7`, blocker = default-install egress via the keyless Amos fallback).
- Fresh GO review: conditional verdict over `66cabd7..2c5e660` + surfaces; its three conditional fixes are the WP-r2 wave (`55dc51f`), each re-verified post-fix.
- Entry schema mirrors `plugin-catalog/ram-first-context.yaml` field shape; catalog README entry schema and admission rules 1–16 apply.
