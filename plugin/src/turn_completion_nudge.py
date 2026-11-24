"""turn-completion-nudge plugin.

Hook registration for post_llm_call; fail-open behavior throughout; metrics for outcome accounting; Jev/OpenRouter decision scoring wired in.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
import threading
import urllib.error
import urllib.request
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

_NUDGE_MESSAGE = (
    "It looks like you stopped mid-turn. Do you want to continue with the next step?"
)
_NUDGE_PREFIX = _NUDGE_MESSAGE
# A turn whose request IS the injected nudge text is nudge-driven; after this
# many consecutive chain hops, stop nudging (unbounded loops otherwise).
_MAX_CONSECUTIVE_NUDGES = 2

# Bound per-turn guard: turn ids already nudged this process lifetime.
# Lives on the handler instance (captured at register time), not at module
# level, so each plugin load starts clean and the structure stays bounded.
_MAX_NUDGED_TURN_IDS = 1024
# Back-compat alias referenced by some callers/tests.
_NUDGED_TURN_IDS = _MAX_NUDGED_TURN_IDS

# Scorer lane endpoints and defaults.
_OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
_DEFAULT_AMOS_URL = "https://api.amos.scira.ai/v1/systemone"
_DEFAULT_SCORER_MODEL = "respan/span-01-lite-20260925:free"
_DEFAULT_AMOS_MODEL = "medium"
_DEFAULT_THRESHOLD = 0.2
_DEFAULT_TIMEOUT_S = 4

# Last model id served by a scorer (process-local, best-effort observability).
_SERVED_MODEL: str | None = None
# Last lane config read live in this process (process-local).
_LAST_LANE_CONFIG: dict[str, Any] = {}

# Persisted metrics schema.
_METRICS_SCHEMA_VERSION = 2
_HOOK_CALL_SCOPE_V2 = "enabled_post_llm_call_invocations_v2"

# Persistence serialization: writers hold _PERSIST_LOCK only around the
# "is this seq newer?" check and the atomic file replace, never while holding
# Metrics._lock. This guarantees an older snapshot cannot overwrite a newer one
# even when concurrent recorders persist out of scheduling order. I/O happens
# under _PERSIST_LOCK (serializing writes) but outside Metrics._lock.
_PERSIST_LOCK = threading.Lock()
_LAST_PERSISTED_SEQ = 0


def _state_path() -> str:
    """Return the metrics state file path inside the plugin directory (resolved at call time)."""
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(plugin_dir, "state", "metrics.json")


def _is_nonneg_int_not_bool(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_pos_int_not_bool(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _default_buckets() -> dict[str, int]:
    return {
        "skipped": 0,
        "scored": 0,
        "nudge_injected": 0,
        "no_nudge": 0,
        "error": 0,
        "dormant": 0,
        "lane_openrouter": 0,
        "lane_amos": 0,
        "lane_failed": 0,
    }


def _normalize_buckets(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    buckets = _default_buckets()
    for k, v in raw.items():
        if not isinstance(k, str) or not _is_nonneg_int_not_bool(v):
            return None
        buckets[k] = int(v)
    return buckets


def _load_persisted_snapshot(path: str | None = None) -> dict[str, Any] | None:
    """Load, validate and migrate a persisted metrics snapshot.

    Returns None on missing/invalid content (fail-open). v1 snapshots used
    total_calls as the bucket-event total; migration retains that value as
    legacy_metric_events/metric_events, starts accurate v2 hook-call total_calls
    at 0, and records an explicit migration epoch. Historical hook-call counts
    are never fabricated.
    """
    global _LAST_PERSISTED_SEQ
    if path is None:
        path = _state_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None

        schema_version = data.get("schema_version", 1)
        buckets = _normalize_buckets(data.get("buckets"))
        if buckets is None:
            return None
        metric_events = sum(buckets.values())
        last_updated = data.get("last_updated")
        if not isinstance(last_updated, str):
            last_updated = ""
        seq = data.get("snapshot_seq", 0)
        if not _is_nonneg_int_not_bool(seq):
            seq = 0

        if schema_version == 1:
            total_calls_raw = data.get("total_calls")
            if not _is_nonneg_int_not_bool(total_calls_raw):
                return None
            # v1 invariant: total_calls was the bucket-event total.
            if int(total_calls_raw) != metric_events:
                return None
            legacy_metric_events = int(total_calls_raw)
            snapshot = {
                "schema_version": _METRICS_SCHEMA_VERSION,
                "total_calls": 0,  # accurate v2 hook calls start at migration epoch
                "metric_events": metric_events,
                "legacy_metric_events": legacy_metric_events,
                "buckets": buckets,
                "invariant_ok": metric_events == sum(buckets.values()),
                "last_updated": last_updated,
                "count_scope": _HOOK_CALL_SCOPE_V2,
                "count_epoch": "migrated_from_v1",
                "snapshot_seq": seq,
            }
        elif schema_version == _METRICS_SCHEMA_VERSION:
            total_calls = data.get("total_calls")
            metric_events_raw = data.get("metric_events")
            if not _is_nonneg_int_not_bool(total_calls):
                return None
            if not _is_nonneg_int_not_bool(metric_events_raw):
                return None
            if int(metric_events_raw) != metric_events:
                return None
            legacy_metric_events = data.get("legacy_metric_events", 0)
            if not _is_nonneg_int_not_bool(legacy_metric_events):
                legacy_metric_events = 0
            count_scope = data.get("count_scope")
            if not isinstance(count_scope, str) or not count_scope:
                count_scope = _HOOK_CALL_SCOPE_V2
            count_epoch = data.get("count_epoch")
            if not isinstance(count_epoch, str) or not count_epoch:
                count_epoch = "v2"
            snapshot = {
                "schema_version": _METRICS_SCHEMA_VERSION,
                "total_calls": int(total_calls),
                "metric_events": metric_events,
                "legacy_metric_events": int(legacy_metric_events),
                "buckets": buckets,
                "invariant_ok": metric_events == sum(buckets.values()),
                "last_updated": last_updated,
                "count_scope": count_scope,
                "count_epoch": count_epoch,
                "snapshot_seq": seq,
            }
        else:
            return None

        with _PERSIST_LOCK:
            if seq > _LAST_PERSISTED_SEQ:
                _LAST_PERSISTED_SEQ = seq
        return snapshot
    except (OSError, ValueError, TypeError) as exc:  # fail-open
        logger.debug("turn-completion-nudge: failed to load persisted metrics: %s", exc)
        return None


def _persist_snapshot(snapshot: dict[str, Any]) -> None:
    """Persist a metrics snapshot atomically to state/metrics.json (fail-open).

    Serialization guarantee: snapshots carry a monotonic snapshot_seq assigned
    under Metrics._lock. Writers may perform file I/O concurrently; the final
    commit is serialized by _PERSIST_LOCK and only applied when snapshot_seq is
    newer than the last persisted seq. This prevents older snapshots from
    overwriting newer state even under out-of-order writer scheduling.
    Metrics._lock is never held during file I/O.
    """
    global _LAST_PERSISTED_SEQ
    path = _state_path()
    try:
        seq = snapshot.get("snapshot_seq", 0)
        if not _is_nonneg_int_not_bool(seq):
            seq = 0
        with _PERSIST_LOCK:
            if seq <= _LAST_PERSISTED_SEQ:
                return
        state_dir = os.path.dirname(path)
        os.makedirs(state_dir, exist_ok=True)
        payload = {
            "schema_version": _METRICS_SCHEMA_VERSION,
            "total_calls": int(snapshot.get("total_calls", 0)),
            "metric_events": int(snapshot.get("metric_events", 0)),
            "legacy_metric_events": int(snapshot.get("legacy_metric_events", 0)),
            "buckets": dict(snapshot.get("buckets", {})),
            "invariant_ok": bool(snapshot.get("invariant_ok", False)),
            "last_updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "count_scope": str(snapshot.get("count_scope", _HOOK_CALL_SCOPE_V2)),
            "count_epoch": str(snapshot.get("count_epoch", "v2")),
            "snapshot_seq": int(seq),
        }
        fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".metrics.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            with _PERSIST_LOCK:
                if seq <= _LAST_PERSISTED_SEQ:
                    return
                os.replace(tmp, path)
                _LAST_PERSISTED_SEQ = seq
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    except (OSError, TypeError, ValueError) as exc:  # fail-open; never raised into hook
        logger.debug("turn-completion-nudge: failed to persist metrics: %s", exc)

_PLACEHOLDER_KEY_FRAGMENTS = ("replace", "your-", "your_", "changeme", "change-me", "test-key", "placeholder", "insert", "sk-or-...")
_PLACEHOLDER_KEY_WARNED = False


def _read_openrouter_key() -> str | None:
    """Read the OpenRouter key via the Hermes secret scope, answer as-is.

    get_secret already reads the declared env var on single-profile installs;
    it returns None only when one process serves several profiles and this
    profile has no key of its own. That refusal is load-bearing: re-reading
    the process env here would hand the turn the launch profile's key, which
    then gets used and billed on this profile's traffic. So the scope's
    answer is returned exactly as given — the only fallback is for Hermes
    builds that lack agent.secret_scope entirely (ImportError).
    """
    try:
        from agent.secret_scope import get_secret  # type: ignore[import-not-found]
    except ImportError:
        return os.environ.get("OPENROUTER_API_KEY")
    return get_secret("OPENROUTER_API_KEY")


def _validate_openrouter_api_key(api_key: str | None) -> str | None:
    """Return a log-ready one-line problem if OPENROUTER_API_KEY is unset,
    empty, or matches a template/placeholder shape (e.g. "REPLACE_*",
    "your-key"); None when it looks like an issued key. Never echo raw key
    text in the returned message."""
    if api_key is None or not str(api_key).strip():
        return "OPENROUTER_API_KEY is not set"
    lowered = str(api_key).strip().lower()
    if any(fragment in lowered for fragment in _PLACEHOLDER_KEY_FRAGMENTS):
        return "OPENROUTER_API_KEY appears to be a placeholder/template value"
    return None


class Metrics:
    """Thread-safe outcome counters with persisted v2 schema.

    Semantics (v2):
    - total_calls: enabled post_llm_call hook invocations, counted exactly once
      per invocation (including skip, dormant, lane failure, and outer error
      paths). Disabled-hook no-op calls are not counted. This counter starts at 0
      for the migration epoch when upgrading a v1 snapshot; historical hook-call
      counts are never fabricated.
    - metric_events: number of bucket-record increments; invariant is
      metric_events == sum(buckets).
    - legacy_metric_events: retained v1 bucket-event total when migrated.

    Persistence serialization: each mutation increments snapshot_seq under
    Metrics._lock, then persists outside the lock. _persist_snapshot only writes
    if snapshot_seq is newer than the last persisted seq, preventing
    out-of-order writer scheduling from overwriting newer state. File I/O is
    never performed while holding Metrics._lock.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._total_calls: int = 0  # enabled hook invocations (v2 scope)
        self._metric_events: int = 0  # bucket-record increments
        self._legacy_metric_events: int = 0
        self._count_scope: str = _HOOK_CALL_SCOPE_V2
        self._count_epoch: str = "v2"
        self._snapshot_seq: int = 0
        self._buckets: dict[str, int] = _default_buckets()
        self._recent_scores: "deque[float]" = deque(maxlen=5)
        # Load persisted snapshot into this instance (fail-open).
        try:
            loaded = _load_persisted_snapshot()
            if loaded is not None:
                self._total_calls = int(loaded.get("total_calls", 0))
                self._metric_events = int(loaded.get("metric_events", 0))
                self._legacy_metric_events = int(loaded.get("legacy_metric_events", 0))
                self._count_scope = str(loaded.get("count_scope", _HOOK_CALL_SCOPE_V2))
                self._count_epoch = str(loaded.get("count_epoch", "v2"))
                self._snapshot_seq = int(loaded.get("snapshot_seq", 0))
                merged = _default_buckets()
                merged.update(dict(loaded.get("buckets", {})))
                self._buckets = merged
        except (OSError, ValueError, TypeError):  # fail-open
            self._total_calls = 0
            self._metric_events = 0
            self._legacy_metric_events = 0
            self._count_scope = _HOOK_CALL_SCOPE_V2
            self._count_epoch = "v2"
            self._snapshot_seq = 0
            self._buckets = _default_buckets()

    def _assert_invariant(self) -> None:
        total = sum(self._buckets.values())
        if self._metric_events != total:
            raise AssertionError(
                f"metrics invariant violated: metric_events={self._metric_events} "
                f"!= sum(buckets)={total} buckets={self._buckets!r}"
            )

    def _snapshot_locked(self) -> dict[str, Any]:
        total = sum(self._buckets.values())
        return {
            "schema_version": _METRICS_SCHEMA_VERSION,
            "total_calls": self._total_calls,
            "metric_events": self._metric_events,
            "legacy_metric_events": self._legacy_metric_events,
            "buckets": dict(self._buckets),
            "invariant_ok": self._metric_events == total,
            "recent_scores": list(self._recent_scores),
            "count_scope": self._count_scope,
            "count_epoch": self._count_epoch,
            "snapshot_seq": self._snapshot_seq,
        }

    def _persist_current(self) -> None:
        with self._lock:
            snap = self._snapshot_locked()
        # Persist AFTER releasing Metrics._lock; _persist_snapshot is fail-open.
        _persist_snapshot(snap)

    def record(self, outcome: str) -> None:
        """Increment metric_events and the bucket for outcome."""
        with self._lock:
            if outcome not in self._buckets:
                self._buckets[outcome] = 0
            self._metric_events += 1
            self._buckets[outcome] = self._buckets.get(outcome, 0) + 1
            self._snapshot_seq += 1
            self._assert_invariant()
            snap = self._snapshot_locked()
        _persist_snapshot(snap)

    def record_hook_call(self) -> None:
        """Count one enabled post_llm_call invocation (v2 total_calls scope)."""
        with self._lock:
            self._total_calls += 1
            self._snapshot_seq += 1
            snap = self._snapshot_locked()
        _persist_snapshot(snap)

    def record_score(self, score: Any) -> None:
        """Record a recent score (process-local, capped at 5)."""
        try:
            value = float(score)
        except (TypeError, ValueError):
            return
        with self._lock:
            self._recent_scores.append(value)

    def snapshot(self) -> dict[str, Any]:
        """Return a copy of counters plus invariant and scope metadata."""
        with self._lock:
            return self._snapshot_locked()


_METRICS = Metrics()


def check_metrics_invariant(metrics: Metrics) -> bool:
    """Return True if the metrics invariant holds."""
    snap = metrics.snapshot()
    return bool(snap.get("invariant_ok", False))


def clip_user_request(text: Any) -> str:
    """Return the first 1200 characters of text (sanitized payload only)."""
    return str(text or "")[:1200]


def clip_final_message(text: Any) -> str:
    """Return the first 1500 characters of text (sanitized payload only)."""
    return str(text or "")[:1500]


def _tool_call_name(call: Any) -> str:
    """Best effort name of a single tool-call entry."""
    if isinstance(call, dict):
        return str(
            call.get("name")
            or call.get("tool")
            or (call.get("function") or {}).get("name")
            or ""
        )
    return str(getattr(call, "name", "") or "")


def _assistant_tool_calls(conversation_history: Any) -> list[Any]:
    """Collect tool_calls entries from assistant messages in history."""
    calls: list[Any] = []
    if not isinstance(conversation_history, (list, tuple)):
        return calls
    for msg in conversation_history:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, (list, tuple)):
            calls.extend(tool_calls)
    return calls


def count_tool_calls(kwargs: dict[str, Any]) -> int:
    """Count tool calls from kwargs or the delivered conversation history.

    Sanitized payloads only; best effort. The kwargs tool_calls / count keys
    are checked first; otherwise assistant tool_calls entries in
    conversation_history (the real payload shape from turn_finalizer.py) are
    counted.
    """
    for key in ("tool_calls", "tool_call_count", "num_tool_calls"):
        value = kwargs.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, (list, tuple)):
            return len(value)
    return len(_assistant_tool_calls(kwargs.get("conversation_history")))


def any_write_edit_bash(kwargs: dict[str, Any]) -> bool:
    """Return True if any write/edit/bash-like tool call appears.

    Notes: operates on sanitized payloads only (no secrets, no transcripts
    beyond clipped text). Best-effort heuristic over the kwargs tool_calls
    entries, the saw_* flags, and assistant tool_calls entries in
    conversation_history (the real payload shape).
    """
    tool_calls = kwargs.get("tool_calls")
    if isinstance(tool_calls, (list, tuple)):
        for call in tool_calls:
            name_l = _tool_call_name(call).lower()
            if (
                "write" in name_l
                or "edit" in name_l
                or "bash" in name_l
                or "shell" in name_l
            ):
                return True
    for key in ("saw_write", "saw_edit", "saw_bash", "has_write_edit_bash"):
        if bool(kwargs.get(key)):
            return True
    for call in _assistant_tool_calls(kwargs.get("conversation_history")):
        name_l = _tool_call_name(call).lower()
        if (
            "write" in name_l
            or "edit" in name_l
            or "bash" in name_l
            or "shell" in name_l
        ):
            return True
    return False


def _read_enabled(ctx: Any, kwargs: dict[str, Any]) -> bool:
    """Read the enabled flag (default true) using ctx config when available."""
    for source in (kwargs.get("config"), kwargs.get("plugin_config")):
        if isinstance(source, dict) and "enabled" in source:
            return bool(source.get("enabled"))
    for attr in ("config", "plugin_config"):
        source = getattr(ctx, attr, None)
        if isinstance(source, dict) and "enabled" in source:
            return bool(source.get("enabled"))
    for method_name in ("get_config", "get_plugin_config", "config_get"):
        method = getattr(ctx, method_name, None)
        if callable(method):
            try:
                value = method("enabled")
                if value is not None:
                    return bool(value)
            except TypeError:
                try:
                    value = method()
                    if isinstance(value, dict) and "enabled" in value:
                        return bool(value.get("enabled"))
                except Exception:
                    pass
            except Exception:
                pass
    return True


_UNSET = object()


def _config_get(ctx: Any, kwargs: dict[str, Any], key: str, default: Any = None) -> Any:
    """Read a config value live, supporting kwargs dicts, ctx attrs, and ctx.get_config."""
    for source in (kwargs.get("config"), kwargs.get("plugin_config")):
        if isinstance(source, dict) and key in source:
            return source.get(key)
    for attr in ("config", "plugin_config"):
        source = getattr(ctx, attr, None) if ctx is not None else None
        if isinstance(source, dict) and key in source:
            return source.get(key)
    method = getattr(ctx, "get_config", None) if ctx is not None else None
    if callable(method):
        value = _UNSET
        try:
            value = method(key, _UNSET)
        except TypeError:
            try:
                value = method(key)
            except TypeError:
                try:
                    full = method()
                    if isinstance(full, dict) and key in full:
                        value = full.get(key)
                    else:
                        value = _UNSET
                except Exception:
                    value = _UNSET
            except Exception:
                value = _UNSET
        except Exception:
            value = _UNSET
        if value is not _UNSET:
            return value
        # Some implementations ignore default and return None for missing keys.
        try:
            value = method(key)
        except Exception:
            value = _UNSET
        if value is not _UNSET:
            return value
    return default


def _parse_strict_bool(value: Any, default: bool) -> bool:
    """Strict bool parse: strings accept only true/1/yes (case-insensitive, whitespace-stripped); non-strings fall back to bool()."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value) if value is not None else default


def _validate_threshold(value: Any) -> float:
    """Accept only finite numeric threshold in [0,1]; reject bool/NaN/out-of-range.

    Invalid values warn and fall back to the documented safe default. No silent
    clamping: out-of-range finite values are rejected.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning(
            "turn-completion-nudge: invalid threshold %r; using default %s",
            value,
            _DEFAULT_THRESHOLD,
        )
        return _DEFAULT_THRESHOLD
    threshold = float(value)
    if not (threshold == threshold and threshold >= 0.0 and threshold <= 1.0):  # NaN/inf/range
        logger.warning(
            "turn-completion-nudge: invalid threshold %r; using default %s",
            value,
            _DEFAULT_THRESHOLD,
        )
        return _DEFAULT_THRESHOLD
    return threshold


def _validate_timeout_s(value: Any) -> int:
    """Accept only positive integer timeout_s; reject bool/float/non-integral/<=0.

    Invalid values warn and fall back to the documented safe default.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        # Accept integral numeric strings only if they are strict integer forms.
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                parsed = int(text)
                if parsed > 0:
                    return parsed
        logger.warning(
            "turn-completion-nudge: invalid timeout_s %r; using default %s",
            value,
            _DEFAULT_TIMEOUT_S,
        )
        return _DEFAULT_TIMEOUT_S
    if value <= 0:
        logger.warning(
            "turn-completion-nudge: invalid timeout_s %r; using default %s",
            value,
            _DEFAULT_TIMEOUT_S,
        )
        return _DEFAULT_TIMEOUT_S
    return int(value)


def _read_lane_config(ctx: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Read the full lane config live (enabled, send_turn_text, models, threshold, timeout, amos fallback)."""
    enabled = _config_get(ctx, kwargs, "enabled", True)
    if enabled is None:
        enabled = True
    send_turn_text = _config_get(ctx, kwargs, "send_turn_text", False)
    if send_turn_text is None:
        send_turn_text = False
    scorer_model = _config_get(
        ctx, kwargs, "scorer_model", _DEFAULT_SCORER_MODEL
    )
    amos_model = _config_get(ctx, kwargs, "amos_model", _DEFAULT_AMOS_MODEL)
    amos_fallback = _config_get(ctx, kwargs, "amos_fallback", False)
    amos_base_url = _config_get(ctx, kwargs, "amos_base_url", _DEFAULT_AMOS_URL)
    threshold = _validate_threshold(_config_get(ctx, kwargs, "threshold", _DEFAULT_THRESHOLD))
    timeout_s = _validate_timeout_s(_config_get(ctx, kwargs, "timeout_s", _DEFAULT_TIMEOUT_S))
    return {
        "enabled": _parse_strict_bool(enabled, True),
        "send_turn_text": _parse_strict_bool(send_turn_text, False),
        "scorer_model": str(scorer_model) if scorer_model is not None else _DEFAULT_SCORER_MODEL,
        "amos_model": str(amos_model) if amos_model is not None else _DEFAULT_AMOS_MODEL,
        "amos_fallback": _parse_strict_bool(amos_fallback, False),
        "amos_base_url": str(amos_base_url) if amos_base_url is not None else _DEFAULT_AMOS_URL,
        "threshold": threshold,
        "timeout_s": timeout_s,
    }


def _send_turn_text_opted_out(ctx: Any, kwargs: dict[str, Any]) -> bool:
    """True when send_turn_text is NOT strictly true. Absent -> opted out (dormant-by-default) in EVERY context: one default path, no environment sniffing."""
    for source in (kwargs.get("config"), kwargs.get("plugin_config")):
        if isinstance(source, dict) and "send_turn_text" in source:
            return not _parse_strict_bool(source.get("send_turn_text"), False)
    if ctx is not None:
        for attr in ("config", "plugin_config"):
            source = getattr(ctx, attr, None)
            if isinstance(source, dict) and "send_turn_text" in source:
                return not _parse_strict_bool(source.get("send_turn_text"), False)
        method = getattr(ctx, "get_config", None)
        if callable(method):
            try:
                value = method("send_turn_text")
                if value is not None:
                    return not _parse_strict_bool(value, False)
            except Exception:
                pass
    # Absent everywhere -> dormant. No PYTEST_CURRENT_TEST env sniffing: the
    # production default IS the unit-test default (config passed explicitly).
    return True


def _derive_turn_id(kwargs: dict[str, Any]) -> str | None:
    """Derive a per-turn id from kwargs for the once-per-turn guard."""
    for key in ("turn_id", "request_id"):
        value = kwargs.get(key)
        if value is None:
            continue
        text = str(value)
        if text:
            return text
    return None


def _get_final_text(kwargs: dict[str, Any]) -> str:
    """Extract final assistant text from common kwargs shapes.

    Reads the real fire-site key assistant_response first (str |
    dict{"content", "text"} | None), then legacy keys as fallback.
    """
    for key in (
        "assistant_response",
        "final_message",
        "final_text",
        "assistant_message",
        "response",
        "text",
    ):
        value = kwargs.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            content = value.get("content") or value.get("text")
            if isinstance(content, str) and content:
                return content
    message = kwargs.get("message")
    if isinstance(message, dict):
        content = message.get("content") or message.get("text")
        if isinstance(content, str):
            return content
    return ""


def _get_user_request(kwargs: dict[str, Any]) -> str:
    """Extract user request text from common kwargs shapes."""
    for key in ("user_request", "user_message", "prompt", "input", "query"):
        value = kwargs.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            content = value.get("content") or value.get("text")
            if isinstance(content, str) and content:
                return content
    messages = kwargs.get("messages")
    if isinstance(messages, (list, tuple)):
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content") or msg.get("text")
                if isinstance(content, str) and content:
                    return content
    return ""


class JevScoreError(Exception):
    """Raised when Jev/OpenRouter scoring fails (fail-open)."""


def _build_state(user_request: str, final_message: str, tool_call_count: int, any_write_edit_bash: bool) -> str:
    """Sanitized, clipped scorer state (no secrets, no full transcripts)."""
    return (
        f"User request: {clip_user_request(user_request)}\n"
        f"Final message: {clip_final_message(final_message)}\n"
        f"Tool call count: {tool_call_count}\n"
        f"Has write/edit/bash: {bool(any_write_edit_bash)}"
    )


def _noul_body(model: str, state: str) -> dict[str, Any]:
    return {
        "model": model,
        "state": state,
        "questions": {
            "q1": {
                "type": "noul",
                "instructions": "Score whether the assistant final message completes the user request.",
                "criteria": {
                    "true": "The turn is complete; no further action is needed.",
                    "false": "The turn is incomplete; the agent should continue.",
                },
            }
        },
    }


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout_s: int) -> dict[str, Any]:
    """POST JSON, return parsed body or raise JevScoreError on any transport/parse failure."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers=headers, method="POST",
    )
    resp = None
    try:
        resp = urllib.request.urlopen(req, timeout=timeout_s)
        raw = resp.read()
    except Exception as exc:  # noqa: BLE001 - fail-open
        raise JevScoreError(str(exc)) from exc
    finally:
        if resp is not None and hasattr(resp, "close"):
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
    try:
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - fail-open
        raise JevScoreError(str(exc)) from exc


def _extract_noul(parsed: dict[str, Any]) -> float:
    try:
        noul = parsed["answers"]["q1"]["noul"]
    except (KeyError, TypeError, AttributeError) as exc:
        raise JevScoreError(str(exc)) from exc
    if isinstance(noul, bool) or not isinstance(noul, (int, float)):
        raise JevScoreError("non-numeric noul value")
    value = float(noul)
    if value < 0.0:
        value = 0.0
    elif value > 1.0:
        value = 1.0
    return value


def _extract_served_model(parsed: Any, default: str) -> str:
    if isinstance(parsed, dict):
        for key in ("model", "served_model", "id"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                return value
    return default


def _openrouter_score(user_request: Any, final_message: Any, tool_call_count: Any, any_write_edit_bash: Any, scorer_model: str, timeout_s: int) -> float:
    global _SERVED_MODEL, _PLACEHOLDER_KEY_WARNED
    api_key = _read_openrouter_key()
    issue = _validate_openrouter_api_key(api_key)
    if issue is not None:
        if not _PLACEHOLDER_KEY_WARNED:
            logger.warning(issue)
            _PLACEHOLDER_KEY_WARNED = True
        raise JevScoreError(issue)
    state = _build_state(user_request, final_message, tool_call_count, any_write_edit_bash)
    parsed = _post_json(
        _OPENROUTER_URL,
        _noul_body(scorer_model, state),
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout_s,
    )
    score = _extract_noul(parsed)
    _SERVED_MODEL = _extract_served_model(parsed, scorer_model)
    return score


def _amos_score(user_request: Any, final_message: Any, tool_call_count: Any, any_write_edit_bash: Any, amos_model: str, timeout_s: int, amos_base_url: str = _DEFAULT_AMOS_URL) -> float:
    global _SERVED_MODEL
    state = _build_state(user_request, final_message, tool_call_count, any_write_edit_bash)
    parsed = _post_json(
        amos_base_url,
        _noul_body(amos_model, state),
        {"Content-Type": "application/json"},
        timeout_s,
    )
    score = _extract_noul(parsed)
    _SERVED_MODEL = _extract_served_model(parsed, amos_model)
    return score


def _jev_score(**kwargs: Any) -> float:
    """Score a turn via OpenRouter decisions (respan/span-01-lite:free, noul).

    Reads the API key only from OPENROUTER_API_KEY. Any failure raises
    JevScoreError (fail-open). Model and timeout are read from kwargs
    (config-driven) with the pinned defaults as fallback.
    """
    return _openrouter_score(
        kwargs.get("user_request", ""),
        kwargs.get("final_message", ""),
        kwargs.get("tool_call_count", 0),
        kwargs.get("any_write_edit_bash", False),
        kwargs.get("scorer_model", _DEFAULT_SCORER_MODEL),
        kwargs.get("timeout_s", _DEFAULT_TIMEOUT_S),
    )


class TurnCompletionNudge:
    """Fail-open post_llm_call handler with ctx captured at register time.

    The Hermes dispatcher forwards payload keys only to hook callbacks
    (plugins_dispatch.py never injects ctx), so ctx is captured once at
    registration: register(ctx) builds an instance holding self.ctx and
    registers the bound post_llm_call method. kwargs carries only the payload
    (see the turn_finalizer.py fire site: assistant_response, user_message,
    conversation_history, turn_id, ...).
    """

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._guard_lock = threading.RLock()
        self._nudged_ids: set[str] = set()
        self._nudged_order: deque[str] = deque()
        self._nudge_chain_depth = 0

    def _guard_turn(self, turn_id: str | None) -> bool:
        """Return True if the turn may be nudged (first eligible nudge).

        Moved after skip / scoring eligibility so skipped or scoring-failed
        turns never consume a nudge slot. Bounded: evicts the oldest id once
        the cap is reached so the structure cannot grow without bound over a
        process lifetime.
        """
        if turn_id is None:
            return True
        with self._guard_lock:
            if turn_id in self._nudged_ids:
                return False
            if len(self._nudged_ids) >= _MAX_NUDGED_TURN_IDS:
                old = self._nudged_order.popleft()
                self._nudged_ids.discard(old)
            self._nudged_order.append(turn_id)
            self._nudged_ids.add(turn_id)
            return True

    def post_llm_call(self, **kwargs: Any) -> Any:
        """Fail-open post_llm_call handler.

        ctx is captured at register time (self.ctx); kwargs.get("ctx") remains
        as a harmless fallback. The whole body is wrapped in try/except; this
        hook never blocks and never raises.
        """
        counted_hook_call = False
        try:
            ctx = self.ctx if self.ctx is not None else kwargs.get("ctx")

            if not _read_enabled(ctx, kwargs):
                return None

            # Enabled hook invocation: count exactly once, including skip,
            # dormant, lane failure, and outer-error paths. Disabled no-op above
            # is intentionally not counted.
            try:
                _METRICS.record_hook_call()
                counted_hook_call = True
            except Exception:  # fail-open
                pass

            tier = str(kwargs.get("tier") or "").strip().lower()
            final_text = _get_final_text(kwargs)
            user_request = _get_user_request(kwargs)

            # Skip conditions: eligibility is decided before the per-turn guard
            # so light-tier / empty-final / question-ending turns never consume
            # a nudge slot.
            if (
                tier == "light"
                or not final_text
                or final_text.rstrip().endswith("?")
                or not user_request
            ):
                _METRICS.record("skipped")
                return None

            # Read the full lane config live via ctx.get_config (per call).
            config = _read_lane_config(ctx, kwargs)
            _LAST_LANE_CONFIG.update(config)

            # Dormant lane: send_turn_text explicitly opted out via config ->
            # no scoring calls, no egress.
            if _send_turn_text_opted_out(ctx, kwargs):
                _METRICS.record("dormant")
                logger.info(
                    "turn-completion-nudge: dormant (send_turn_text=false); "
                    "no scoring calls, no egress"
                )
                return None

            # Lane ladder: OpenRouter first. The keyless Amos/Scira hop is a
            # SECOND opt-in (amos_fallback) — never a silent second service:
            # a user who opted into OpenRouter alone never egresses there.
            lane = None
            try:
                p = _jev_score(
                    user_request=clip_user_request(user_request),
                    final_message=clip_final_message(final_text),
                    tool_call_count=count_tool_calls(kwargs),
                    any_write_edit_bash=any_write_edit_bash(kwargs),
                    scorer_model=config["scorer_model"],
                    timeout_s=config["timeout_s"],
                )
                lane = "lane_openrouter"
            except Exception:
                if not config["amos_fallback"]:
                    _METRICS.record("lane_failed")
                    logger.warning(
                        "turn-completion-nudge: OpenRouter lane failed and amos_fallback is not "
                        "enabled; no further egress (fail-open). Set amos_fallback: true to "
                        "allow the keyless Amos/Scira hop."
                    )
                    return None
                try:
                    p = _amos_score(
                        user_request=clip_user_request(user_request),
                        final_message=clip_final_message(final_text),
                        tool_call_count=count_tool_calls(kwargs),
                        any_write_edit_bash=any_write_edit_bash(kwargs),
                        amos_model=config["amos_model"],
                        timeout_s=config["timeout_s"],
                        amos_base_url=config["amos_base_url"],
                    )
                    lane = "lane_amos"
                except Exception:
                    _METRICS.record("lane_failed")
                    logger.warning(
                        "turn-completion-nudge: all scorer lanes failed (fail-open)"
                    )
                    return None
            _METRICS.record(lane)
            _METRICS.record_score(p)
            logger.info(
                "turn-completion-nudge: %s score=%s model=%s",
                lane, p, _SERVED_MODEL or "unknown",
            )

            # Threshold: p < threshold means low confidence the turn is
            # complete -> nudge.
            if isinstance(p, (int, float)) and float(p) < config["threshold"]:
                turn_id = _derive_turn_id(kwargs)
                # Consecutive-nudge cap: a request that IS the injected nudge
                # text is itself nudge-driven; bound the chain so a low-scoring
                # loop cannot nudge forever (reviewer round-trip, 2026-11-09).
                chain_hit = user_request.startswith(_NUDGE_PREFIX)
                with self._guard_lock:
                    if not chain_hit:
                        self._nudge_chain_depth = 0
                    elif self._nudge_chain_depth >= _MAX_CONSECUTIVE_NUDGES:
                        _METRICS.record("no_nudge")
                        logger.info(
                            "turn-completion-nudge: consecutive-nudge cap (%d) reached; not nudging again this chain",
                            _MAX_CONSECUTIVE_NUDGES,
                        )
                        return None
                if not self._guard_turn(turn_id):
                    return None
                inject = (
                    getattr(ctx, "inject_message", None) if ctx is not None else None
                )
                if callable(inject):
                    accepted = inject(_NUDGE_MESSAGE)
                    if accepted:
                        with self._guard_lock:
                            self._nudge_chain_depth = (
                                self._nudge_chain_depth + 1
                                if chain_hit
                                else 1
                            )
                        _METRICS.record("nudge_injected")
                    else:
                        _METRICS.record("no_nudge")
                else:
                    _METRICS.record("no_nudge")
            else:
                with self._guard_lock:
                    self._nudge_chain_depth = 0
                _METRICS.record("no_nudge")
            return None
        except Exception:
            logger.warning(
                "turn-completion-nudge: post_llm_call failed open",
                exc_info=True,
            )
            try:
                _METRICS.record("error")
                if not counted_hook_call:
                    # Best-effort: keep hook-call accounting aligned if the
                    # invocation count failed before we recorded it.
                    _METRICS.record_hook_call()
            except Exception:
                pass
            return None


def _setup_status_cli(subparser):
    """Add the ``status`` subcommand under ``hermes turn-completion-nudge``."""
    sub_cmds = subparser.add_subparsers(dest="command")
    status_p = sub_cmds.add_parser("status", help="show plugin status")
    status_p.add_argument("--json", action="store_true", help="machine-readable output")
    status_p.set_defaults(func=_status_cli)


def _status_cli(args):
    """Print the turn-completion-nudge status.

    Counters (total_calls/metric_events/buckets) are persisted across processes.
    Lane config and last_scores are process-local observability.
    """
    import json as _json

    path = _state_path()
    loaded = _load_persisted_snapshot(path)
    if loaded is not None:
        source = "persisted"
        total_calls = int(loaded.get("total_calls", 0))
        metric_events = int(loaded.get("metric_events", sum(dict(loaded.get("buckets", {})).values())))
        legacy_metric_events = int(loaded.get("legacy_metric_events", 0))
        buckets = dict(loaded.get("buckets", {}))
        invariant_ok = metric_events == sum(buckets.values())
        count_scope = str(loaded.get("count_scope", _HOOK_CALL_SCOPE_V2))
        count_epoch = str(loaded.get("count_epoch", "v2"))
        schema_version = int(loaded.get("schema_version", _METRICS_SCHEMA_VERSION))
        try:
            last_updated = datetime.datetime.fromtimestamp(
                os.path.getmtime(path), tz=datetime.timezone.utc
            ).isoformat()
        except Exception:  # noqa: BLE001
            last_updated = loaded.get("last_updated") or ""
    else:
        source = "process"
        proc = _METRICS.snapshot()
        total_calls = int(proc.get("total_calls", 0))
        metric_events = int(proc.get("metric_events", sum(dict(proc.get("buckets", {})).values())))
        legacy_metric_events = int(proc.get("legacy_metric_events", 0))
        buckets = dict(proc.get("buckets", {}))
        invariant_ok = metric_events == sum(buckets.values())
        count_scope = str(proc.get("count_scope", _HOOK_CALL_SCOPE_V2))
        count_epoch = str(proc.get("count_epoch", "v2"))
        schema_version = int(proc.get("schema_version", _METRICS_SCHEMA_VERSION))
        last_updated = ""

    cfg = dict(_LAST_LANE_CONFIG) or {
        "enabled": True,
        "send_turn_text": False,
        "threshold": _DEFAULT_THRESHOLD,
        "scorer_model": _DEFAULT_SCORER_MODEL,
        "amos_model": _DEFAULT_AMOS_MODEL,
        "timeout_s": _DEFAULT_TIMEOUT_S,
    }
    proc_snap = _METRICS.snapshot()
    out = {
        "command": "turn-completion-nudge status",
        "schema_version": schema_version,
        "source": source,
        "last_updated": last_updated,
        "enabled": cfg.get("enabled", True),
        "opted_in": bool(cfg.get("send_turn_text", False)),
        "threshold": cfg.get("threshold", _DEFAULT_THRESHOLD),
        "scorer_model": cfg.get("scorer_model"),
        "amos_model": cfg.get("amos_model"),
        "timeout_s": cfg.get("timeout_s"),
        "last_scores": list(proc_snap.get("recent_scores", [])),
        "last_scores_scope": "process_local",
        "buckets": buckets,
        "total_calls": total_calls,
        "total_calls_scope": count_scope,
        "total_calls_epoch": count_epoch,
        "metric_events": metric_events,
        "legacy_metric_events": legacy_metric_events,
        "invariant_ok": invariant_ok,
        "lane_config_process_local": True,
    }
    if getattr(args, "json", False):
        print(_json.dumps(out, indent=2))
    else:
        print(f"turn-completion-nudge status (counters: {source}; lane config/last_scores: process-local):")
        print(f"  enabled: {out['enabled']}")
        print(f"  opted_in (send_turn_text): {out['opted_in']}")
        print(f"  threshold: {out['threshold']}")
        print(f"  scorer_model: {out['scorer_model']}")
        print(f"  amos_model: {out['amos_model']}")
        print(f"  timeout_s: {out['timeout_s']}")
        print(f"  last_scores (process-local): {out['last_scores']}")
        print(f"  buckets: {out['buckets']}")
        print(f"  total_calls ({out['total_calls_scope']}, epoch={out['total_calls_epoch']}): {out['total_calls']}")
        print(f"  metric_events: {out['metric_events']}")
        print(f"  legacy_metric_events: {out['legacy_metric_events']}")
        print(f"  invariant_ok (metric_events == sum(buckets)): {out['invariant_ok']}")
        print(f"  schema_version: {out['schema_version']}")
        print(f"  source: {out['source']}")
        print(f"  last_updated: {out['last_updated']}")


def register(ctx: Any) -> None:
    """Register the post_llm_call hook; fail-open on registration errors.

    ctx is captured at register time: the handler instance holds self.ctx and
    the bound post_llm_call method is registered, since the dispatcher never
    injects ctx into hook callbacks (payload keys only).
    """
    try:
        handler = TurnCompletionNudge(ctx)
        ctx.register_hook("post_llm_call", handler.post_llm_call)
        # Seed last-seen lane config from persisted settings (process-local).
        try:
            _LAST_LANE_CONFIG.update(_read_lane_config(ctx, {}))
        except Exception:
            _LAST_LANE_CONFIG.update({
                "enabled": True,
                "send_turn_text": False,
                "threshold": _DEFAULT_THRESHOLD,
                "scorer_model": _DEFAULT_SCORER_MODEL,
                "amos_model": _DEFAULT_AMOS_MODEL,
                "timeout_s": _DEFAULT_TIMEOUT_S,
            })
        # CLI status subcommand: hermes turn-completion-nudge status
        ctx.register_cli_command(
            name="turn-completion-nudge",
            help="turn-completion-nudge plugin status and metrics",
            setup_fn=_setup_status_cli,
            handler_fn=_status_cli,
            description="Show turn-completion-nudge lane config and in-process metrics.",
        )
    except Exception:
        logger.warning("turn-completion-nudge: register failed", exc_info=True)
        return
