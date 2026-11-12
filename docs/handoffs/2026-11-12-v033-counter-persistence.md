---
title: turn-completion-nudge v0.3.3 — cross-process counters (the "counter was always 0" defect)
created: 2026-11-12
updated: 2026-11-12
type: entity
tags: [hermes, plugin, turn-completion-nudge, metrics, observability, upstream, catalog]
sources: [projects/hermes-turn-completion-nudge-2026-11-08, projects/turn-completion-nudge-70s-demo-2026-11-10, https://github.com/ether-btc/turn-completion-nudge, https://github.com/NousResearch/hermes-agent/pull/154372]
confidence: high
contradictions: []
---

# turn-completion-nudge v0.3.3 — cross-process counters (2026-11-12)

## The defect

`hermes turn-completion-nudge status` printed `total_calls: 0`, empty buckets and
`invariant_ok: True` on every call, while the plugin was in fact scoring every turn.

Cause: `Metrics` kept its counters in **process memory**, and the status CLI runs in a
**fresh process**, so it always read a newly constructed zero instance. `invariant_ok`
was trivially true (0 == 0). The advertised counter was unreadable **by construction**,
not idle: the gateway log carried 79 scored turns on 2026-11-12 alone (latest
`lane_openrouter score=0.948541`, 09:14:47).

## The fix (v0.3.3)

Commit `8e1fffb85ea2440dee28fdcfc46ad43bb779aaad` on `ether-btc/turn-completion-nudge`:

- `Metrics.record()` persists an atomic snapshot to `<plugin_dir>/state/metrics.json`
  (tempfile in the same directory + `fsync` + `os.replace`), fail-open — it can never
  raise into the `post_llm_call` hook. Exceptions narrowed to
  `OSError/TypeError/ValueError`, consistent with the round-2 review rule against bare
  `except Exception`.
- `Metrics.__init__` rehydrates the persisted counts, so they survive a gateway restart.
- `status` reads the persisted file (`source: persisted|process`), recomputes the
  invariant from the numbers instead of trusting the file's own flag, and reports
  `last_updated`; a missing/corrupt/invalid file falls back to the in-process snapshot
  without crashing.
- `state/` is gitignored; still zero writes outside the plugin directory; stdlib only.
- Tests: +3 (round-trip, corrupt-file fallback, write-failure fail-open) → **68/68**
  (fresh run at filing time).

Explicitly unchanged: scoring, threshold, privacy defaults (`send_turn_text`), the
two-lane ladder, and the nudge-chain cap.

## Proof

Cross-process (unit-level, real interpreter):

```
process A (fresh):  record scored x2 + no_nudge  -> total_calls 3
process B (fresh):  _status_cli()                -> total_calls 3, source: persisted,
                                                    invariant_ok True, last_updated set
```

Live (post-install, this host):

```
$ hermes turn-completion-nudge status
turn-completion-nudge status (counters: persisted; lane config: process-local):
  buckets: {'lane_openrouter': 1, 'no_nudge': 1, ...}
  total_calls: 2      source: persisted      invariant_ok: True
```

`state/metrics.json` at filing time:
`{"buckets":{"lane_openrouter":1,"no_nudge":1,...},"invariant_ok":true,"total_calls":2}`

## Publication state (verified 2026-11-12 ~13:00 CET)

| Item | State |
|---|---|
| Previous catalog PR #151924 | **MERGED** 2026-11-11T21:22:46Z — the entry is live in the catalog, pinned v0.3.2 |
| Fix commit | `8e1fffb` on `ether-btc/turn-completion-nudge` main (mirror pin commit `d0479e6`) |
| Re-pin PR | **#154372 OPEN**, head `95dbb76`, `MERGEABLE`; no CI checks yet (fork-CI pattern, same as #151924) |
| Live install | v0.3.3, 68/68 locally, `hermes plugins list` shows 0.3.3 enabled |
| Kanban hold card | `t_3c7d7b98` completed after the verdict landed |

## Residual: the long-lived gateway process

The gateway PID 499403 (up since 2026-11-11 10:47 CET) still holds **v0.3.2 in memory**.
`reload-plugins` over the control socket re-wired the `post_llm_call` handler
(`adapters_rewired: 2`) but does **not** re-import the module, so gateway-executed turns
write no state file until the process restarts. A fresh process (the CLI proof above)
loads v0.3.3 from disk and persists correctly.

Consequence: the counter becomes populated for ALL turn paths at the next gateway
restart — no further action required, no code change pending. This was deliberately not
forced mid-session: a restart is a live mutation on a process serving 16 profiles.

Related observation, cause **unverified**: desktop-chat turns produced no hook records
between 09:14 and the CLI proof. Two candidates — (a) that process's in-memory v0.3.2
silently skips those turns (skip is silent in 0.3.2), or (b) the desktop path does not
fire `post_llm_call`. After the gateway restart, (a) would show up as `skipped` in the
state file; if the file still shows nothing for desktop turns, (b) is the answer and is
a separate defect worth its own card.

## Why this mattered beyond the counter

The status command is the plugin's advertised observability surface; a surface that
always reports zeros is worse than none, because it reads as "the plugin is idle" while
it is actually working. The fix keeps the fail-open contract intact (a broken disk never
blocks a turn) and keeps the no-writes-outside-plugin-dir promise that the catalog entry
declares.

## Pointers

- Repo: `~/projects/turn-completion-nudge` (HEAD `d0479e6`, clean, == origin/main)
- Live plugin: `~/.hermes/plugins/turn-completion-nudge` (v0.3.3)
- Upstream PR: https://github.com/NousResearch/hermes-agent/pull/154372
- Prior entry: https://github.com/NousResearch/hermes-agent/pull/151924 (merged)
