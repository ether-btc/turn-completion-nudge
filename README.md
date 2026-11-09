# turn-completion-nudge

Hermes plugin: nudges the agent to continue when a turn ends prematurely (planning or announcing next steps it has not done). Fail-open: never blocks a turn. Public catalog entry `ether-btc/turn-completion-nudge`.

## Badges

[![Build](https://img.shields.io/badge/build-passing-brightgreen.svg)](https://github.com/ether-btc/turn-completion-nudge/actions)
[![Version](https://img.shields.io/badge/version-0.3.0-blue.svg)](https://github.com/ether-btc/turn-completion-nudge/releases/tag/v0.3.0)
[![Hermes](https://img.shields.io/badge/hermes-%3E=0.21.3-blue.svg)](https://github.com/NousResearch/hermes-agent)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> Scaffold note: this repo is git-ready locally with no remote configured; badge targets and network push are operator-gated and come later.

## What you get

A standalone Hermes plugin (`kind: standalone`, `requires_hermes: >=0.21.3`) that scores each completed turn and, when the turn looks incomplete, injects a one-off continuation nudge.

- Hooks: `post_llm_call` (fail-open — scoring or network problems never block a turn)
- Commands: `hermes turn-completion-nudge status` (process-local: enabled/opted-in/threshold/last-5-scores/bucket counts)
- Scorer lane ladder: OpenRouter decisions lane first (`scorer_model`, key from `OPENROUTER_API_KEY` only), then one keyless Amos systemone fallback hop on missing key / HTTP failure / timeout; buckets `lane_openrouter` / `lane_amos` / `lane_failed` / `dormant`; served model logged per call
- Privacy: `send_turn_text` defaults to false — dormant by default, no scoring calls, no egress
- Resource hygiene: bounded per-turn guard (cap 1024, drop-oldest), in-process counters only, zero writes outside the plugin directory
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
| `OPENROUTER_API_KEY` | `requires_env`, `secret: true` | OpenRouter API key for the scorer lane. Optional — unset or a template/placeholder value (empty, `REPLACE*`, `your-key*`) fails open to the keyless Amos fallback, and a placeholder-shaped key warns exactly once in the logs. Read only from the declared env var, never from `.env`. |
| `HERMES_AMOS_BASE_URL` | `optional_env` | Amos-hosted systemone fallback endpoint override (keyless, no API key required). Default: `https://api.amos.scira.ai/v1/systemone`. |

Config schema (settings form, read live per call via `ctx.get_config`):

| Setting | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | true | Master switch; false = dormant hook no-op |
| `send_turn_text` | bool | false | Opt in to sending clipped turn text for scoring (see privacy disclosure) |
| `threshold` | float | 0.2 | Score below this triggers a continuation nudge (0–1) |
| `timeout_s` | int | 4 | Per-hop scorer timeout in seconds |
| `scorer_model` | str | `respan/span-01-lite-20260925:free` | OpenRouter decisions model; models can be delisted (rot) — pin or monitor |
| `amos_model` | str | `medium` | Amos systemone fallback model (server may serve smaller — log the served id) |

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
| OpenRouter lane silent | `hermes logs` — an empty or placeholder `OPENROUTER_API_KEY` warns exactly once; the lane fails open to Amos |
| All scorer lanes failed | `lane_failed` bucket increments; the hook returns `None` (fail-open, the turn continues) |
| Debug | `HERMES_*_DEBUG` flags plus `hermes logs`; each scored call logs its lane, score, and served model |

## Attribution

- Author: `ether-btc` (NousResearch catalog entry `ether-btc/turn-completion-nudge`).
- Mechanism inspiration: the public "jev" continuation design (X post) and mu's continuation check; the Hermes-native `post_llm_call` integration is original work.
- House conventions adapted from the official `hop/langfuse` plugin: explicit `requires_env`/`optional_env` declarations with secret flags, template/placeholder key validation, and isolated `HERMES_HOME` test fixtures.

## License

MIT (SPDX: MIT). Copyright (c) 2026 ether-btc. See LICENSE.

Privacy disclosure: turn-completion-nudge never shares your turn text by default. `send_turn_text` is `false` by default, so the plugin runs dormant — one INFO line per enable session explains why, no scoring calls are made, and no egress occurs. When opted in, each scoring call sends exactly one clipped payload (~1200-char user request + ~1500-char assistant message) to the configured scorer endpoint; vendors may retain requests. The OpenRouter key is read only from the declared `OPENROUTER_API_KEY` environment variable — never from `.env` files.
