# MISSION: post-review fix wave 2 — turn-completion-nudge catalog entry

## Mission
Close the three conditional fixes from the independent review (deleg_910e22ab, verdict GO) on
~/projects/turn-completion-nudge so the entry is push-ready, operator still gates the actual push.

## Tools available (in/out of scope, ponytail)
- `patch`/`write_file` + `python3` + `git` — the work itself (OUT: ling-implementer/mini-swe-agent/command-code;
  each fix is a line plus a test, delegation ceremony exceeds the work — ponytail self-application).
- `pytest` — oracle for the privacy guard.
- `hermes plugins validate` — platform gate (15 checks).
- `sha256sum`/`md5sum` — residue dedupe proof.
- OUT of scope: repowise/opensec/ocr (no code-scan or doc-AI need this wave); repomix already used upstream;
  measured-performance-engineering loop not triggered (no performance constraint — correctness wave only).

## Steps (each with output artifact)
1. Shared strict bool parser: extract module-level `_parse_strict_bool(value, default)` mirroring
   `_read_lane_config` semantics (:315-316); wire into `_send_turn_text_opted_out` (:328, :333).
   → artifact: plugin/src/turn_completion_nudge.py
2. Tests: (a) absent key + `PYTEST_CURRENT_TEST` deleted → default install dormant, no scorer calls;
   (b) `send_turn_text: "false"` string → dormant. → plugin/tests/test_turn_completion_nudge.py
3. `cd plugin && python -m pytest tests/` → all pass (54 + new). Record count.
4. `hermes plugins validate` → 15 checks pass.
5. Sync source to live install ~/.hermes/plugins/turn-completion-nudge/src/ (byte-identical).
6. Residue: dedupe duplicate pi5_evidence.json (keep plugin/docs/, delete root docs/ copy after md5 match);
   git add plugin-catalog/ catalog-pr-description.md docs/install-sync-sha256.txt. → clean untracked set.
7. Commit wave, re-pin sha in plugin-catalog/turn-completion-nudge.yaml + catalog-pr-description.md,
   amend/follow-up commit. → final HEAD captured.
8. Update stale self-anchors in ~/.hermes/cache/scratch/tcn-excellence-digest.md (:44-45 → v0.3.0 lines).
9. Save progress: mnemosyne_task_progress + this file updated with DONE markers.

## Constraints
- English only. No push, no merge, no publication — operator-gated (WP-6/§1 contract).
- Behavior contract: pytest default stays opted-in (test harness), production default dormant.
  Every existing test must keep passing unmodified.
- If a premise of any step fails on inspection, reframe here and note it — do not improvise.

## Checkpoints
- Step 3 output = final test count line; step 4 = validator rc 0; step 7 = final sha pin equal to HEAD.

## DONE (2026-11-09 closeout)
- Step 1: `_parse_strict_bool` at plugin/src/turn_completion_nudge.py:292; wired into _read_lane_config (:318-319) and _send_turn_text_opted_out (:331/:336/:342).
- Step 2: tests `test_production_default_absent_key_dormant` + `test_string_false_send_turn_text_dormant` appended.
- Step 3: pytest 56 passed (54 + 2), rc 0 — repo and live install both verified.
- Step 4: hermes plugins validate → 15 checks, "Validation passed.", rc 0.
- Step 5: live install byte-identical (sha256) for src + tests + plugin.yaml.
- Step 6: plugin/docs/pi5_evidence.json duplicate removed (README keeps `docs/`); untracked deliverables committed.
- Step 7: pin convention = latest plugin/-content commit; catalog sha 6b81c43, HEAD 14b1427 (bookkeeping one ahead by construction; pin_note documents this). Commits: 55dc51f, 55dafd2, 6b81c43, 14b1427.
- Step 8: digest re-anchored (fail-open :582-691, timeout_s :453); contradictory empty `provides_commands: []` dropped (house standard: no official plugin declares that field; status CLI stays README+source-declared).
- Remaining gate: operator authorizes the public push (no remote is configured on this clone by design).
