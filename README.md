# turn-completion-nudge

Hermes plugin: nudges the agent to continue when a turn ends prematurely (planning or announcing next steps it has not done). Fail-open: never blocks a turn. Public catalog entry `ether-btc/turn-completion-nudge`.

## Badges

[![Build](https://img.shields.io/badge/build-passing-brightgreen.svg)](https://github.com/ether-btc/turn-completion-nudge/actions)
[![Version](https://img.shields.io/badge/version-0.3.1-blue.svg)](https://github.com/ether-btc/turn-completion-nudge/releases/tag/v0.3.1)
[![Hermes](https://img.shields.io/badge/hermes-%3E=0.21.3-blue.svg)](https://github.com/NousResearch/hermes-agent)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> Scaffold note: this repo is git-ready locally with no remote configured; badge targets and network push are operator-gated and come later.

## What you get

A standalone Hermes plugin (`kind: standalone`, `requires_hermes: >=0.21.3`) that scores each completed turn and, when the turn looks incomplete, injects a one-off continuation nudge.

- Hooks: `post_llm_call` (fail-open — scoring or network problems never block a turn)
- Commands: `hermes turn-completion-nudge status` (persisted counts readable from a separate process: enabled/opted-in/threshold/last-5-scores/bucket counts, source/last_updated)
- Scorer lane ladder: OpenRouter decisions lane first (`scorer_model`, key from `OPENROUTER_API_KEY`); the keyless Amos systemone fallback is a **separate opt-in** (`amos_fallback`, default off — OpenRouter failure without it records `lane_failed` and stops, no second third-party egress); buckets `lane_openrouter` / `lane_amos` / `lane_failed` / `dormant`; served model logged per call
- Privacy: `send_turn_text` defaults to false — dormant by default, no scoring calls, no egress
- Nudge hygiene: bounded per-turn guard (cap 1024, drop-oldest) plus a consecutive-nudge cap of 2 (nudge-driven turns are recognized by the injected text and the chain stops after two hops)
- Resource hygiene: bounded per-turn guard (cap 1024, drop-oldest), persisted counters inside the plugin directory (`state/metrics.json`), zero writes outside the plugin directory
- Hardware fitness: pure stdlib (`urllib`, `json`, `threading`); measured Pi5 round-trips under 5 s — see `docs/pi5_evidence.json`

## Install

One-liner:

    hermes plugins install turn-completion-nudge

Profile-scoped:

    hermes -p <profile> plugins install turn-completion-nudge

Dependency handling: none — pure standard library, no third-party packages, Pi5-friendly.

The plugin installs from the `plugin/` subdirectory of this repo into `~/.hermes/plugins/turn-completion-nudge/`.

## Configuration

Environment variables:

| Variable | Kind | Description |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | `requires_env`, `secret: true` | OpenRouter API key for the scorer lane. Optional — unset or a template/placeholder value (empty, `REPLACE*`, `your-key*`) fails open, and a placeholder-shaped key warns exactly once in the logs. Read via the Hermes secret scope; the scope's answer is used as-is — a multi-profile refusal is never bypassed via env re-read, so one profile's traffic is never billed to another profile's key. The declared env var is read only on Hermes builds without the secret scope — never `.env`. |

Config schema (settings form, read live per call via `ctx.get_config`):

| Setting | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | true | Master switch; false = dormant hook no-op |
| `send_turn_text` | bool | false | Opt in to sending clipped turn text for scoring (see privacy disclosure) |
| `threshold` | float | 0.2 | Score below this triggers a continuation nudge (0–1) |
| `timeout_s` | int | 4 | Per-hop scorer timeout in seconds |
| `scorer_model` | str | `respan/span-01-lite-20260925:free` | OpenRouter decisions model; models can be delisted (rot) — pin or monitor |
| `amos_model` | str | `medium` | Keyless Amos systemone fallback model, used ONLY when `amos_fallback` is true (server may serve smaller — log the served id) |
| `amos_fallback` | bool | false | Second opt-in: allow the keyless Amos/Scira systemone hop on OpenRouter missing key / failure / timeout. Off by default — no second third-party service without an explicit yes |
| `amos_base_url` | str | `https://api.amos.scira.ai/v1/systemone` | Amos systemone endpoint override (non-secret config) |

## On-off switch

- Settings: `enabled: false` turns the hook off (dormant: one INFO line per enable session, no scoring calls, no egress).
- CLI: `hermes plugins disable turn-completion-nudge` unregisters the hook; `hermes plugins enable turn-completion-nudge` restores it.

## Uninstall / No-Residue Guarantees

`hermes plugins remove turn-completion-nudge` removes the plugin directory from `~/.hermes/plugins/turn-completion-nudge/`.

- No background threads or timers survive removal — the bounded guard is in-process and cleared on restart.
- No writes outside the plugin directory; counters are in-process only.
- The config keys under `plugins.entries.turn-completion-nudge.*` (`enabled`, `send_turn_text`…) are left as dormant, harmless keys after removal — remove them manually from settings, or accept them as dormant.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No nudge despite incomplete turns | `hermes turn-completion-nudge status` (process-local): enabled/opted-in, threshold, buckets, last 5 scores |
| OpenRouter lane silent | `hermes logs` — an empty or placeholder `OPENROUTER_API_KEY` warns exactly once; with `amos_fallback: false` (default) no further egress happens |
| All scorer lanes failed | `lane_failed` bucket increments; the hook returns `None` (fail-open, the turn continues) |
| Debug | `HERMES_*_DEBUG` flags plus `hermes logs`; each scored call logs its lane, score, and served model |

## Attribution

- Author: `ether-btc` (NousResearch catalog entry `ether-btc/turn-completion-nudge`).
- Mechanism inspiration: the public "jev" continuation design (X post) and mu's continuation check; the Hermes-native `post_llm_call` integration is original work.
- House conventions adapted from the official `hop/langfuse` plugin: explicit `requires_env`/`optional_env` declarations with secret flags, template/placeholder key validation, and isolated `HERMES_HOME` test fixtures.

## License

MIT (SPDX: MIT). Copyright (c) 2026 ether-btc. See LICENSE.

Privacy disclosure: turn-completion-nudge never shares your turn text by default. `send_turn_text` is `false` by default, so the plugin runs dormant — one INFO line per enable session explains why, no scoring calls are made, and no egress occurs. When opted in, each scoring call sends exactly one clipped payload (~1200-char user request + ~1500-char assistant message) to the configured scorer endpoint; vendors may retain requests. The keyless Amos/Scira fallback is its own opt-in (`amos_fallback`, default false): opting into OpenRouter alone never sends anything to a second third-party service. The OpenRouter key is read via the Hermes secret scope and its answer is used as-is: a multi-profile refusal is never bypassed via an env re-read (that would bill the launch profile's key for this profile's traffic). The declared `OPENROUTER_API_KEY` environment variable is read only on Hermes builds without the secret scope — never from `.env` files.
