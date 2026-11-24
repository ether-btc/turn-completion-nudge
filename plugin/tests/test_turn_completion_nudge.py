import json
import logging
import os
import threading
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.turn_completion_nudge import (
    _NUDGE_MESSAGE,
    JevScoreError,
    Metrics,
    TurnCompletionNudge,
    _amos_score,
    _jev_score,
    any_write_edit_bash,
    check_metrics_invariant,
    clip_final_message,
    clip_user_request,
    count_tool_calls,
)

MODULE = "src.turn_completion_nudge"


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    import src.turn_completion_nudge as mod

    # Isolate persisted metrics per test to avoid cross-test pollution and to make
    # schema/persistence assertions deterministic.
    state_file = tmp_path / "state" / "metrics.json"
    monkeypatch.setattr(mod, "_state_path", lambda: str(state_file))
    monkeypatch.setattr(mod, "_LAST_PERSISTED_SEQ", 0)
    mod._METRICS._total_calls = 0
    mod._METRICS._metric_events = 0
    mod._METRICS._legacy_metric_events = 0
    mod._METRICS._count_scope = mod._HOOK_CALL_SCOPE_V2
    mod._METRICS._count_epoch = "v2"
    mod._METRICS._snapshot_seq = 0
    mod._METRICS._buckets = {k: 0 for k in mod._METRICS._buckets}
    mod._METRICS._recent_scores.clear()
    mod._SERVED_MODEL = None
    mod._LAST_LANE_CONFIG = {}
    mod._PLACEHOLDER_KEY_WARNED = False
    yield
    # Fail-open unlink of persisted state so persistence does not leak between tests.
    try:
        path = mod._state_path()
        if os.path.exists(path):
            os.remove(path)
        state_dir = os.path.dirname(path)
        if os.path.isdir(state_dir) and not os.listdir(state_dir):
            os.rmdir(state_dir)
    except Exception:
        pass


def _mock_response(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    return resp


# ---------- clipping ----------

def test_clip_user_request():
    assert clip_user_request(None) == ""
    assert clip_user_request("abc") == "abc"
    text = "x" * 1300
    assert clip_user_request(text) == "x" * 1200
    assert len(clip_user_request(text)) == 1200
    assert clip_user_request(42) == "42"


def test_clip_final_message():
    assert clip_final_message(None) == ""
    assert clip_final_message("abc") == "abc"
    text = "y" * 1600
    assert clip_final_message(text) == "y" * 1500
    assert len(clip_final_message(text)) == 1500


# ---------- count_tool_calls ----------

def test_count_tool_calls_list():
    assert count_tool_calls({"tool_calls": [1, 2, 3]}) == 3
    assert count_tool_calls({"tool_calls": []}) == 0


def test_count_tool_calls_int():
    assert count_tool_calls({"tool_call_count": 7}) == 7
    assert count_tool_calls({"num_tool_calls": 2}) == 2


def test_count_tool_calls_missing():
    assert count_tool_calls({}) == 0
    assert count_tool_calls({"foo": "bar"}) == 0


def test_count_tool_calls_from_conversation_history():
    assert (
        count_tool_calls(
            {
                "conversation_history": [
                    {"role": "assistant", "tool_calls": [{"name": "a"}, {"name": "b"}]},
                    {"role": "user", "content": "hi"},
                ]
            }
        )
        == 2
    )
    assert count_tool_calls({"conversation_history": []}) == 0
    # kwargs shape wins over history
    assert count_tool_calls(
        {
            "tool_call_count": 5,
            "conversation_history": [
                {"role": "assistant", "tool_calls": [{"name": "x"}]}
            ],
        }
    ) == 5


# ---------- any_write_edit_bash ----------

def test_any_write_edit_bash_write():
    assert any_write_edit_bash({"tool_calls": [{"name": "write_file"}]}) is True


def test_any_write_edit_bash_edit():
    assert any_write_edit_bash({"tool_calls": [{"tool": "edit"}]}) is True


def test_any_write_edit_bash_bash():
    assert any_write_edit_bash({"tool_calls": [{"function": {"name": "bash"}}]}) is True


def test_any_write_edit_bash_shell():
    assert any_write_edit_bash({"tool_calls": [{"name": "shell_exec"}]}) is True


def test_any_write_edit_bash_flags():
    for key in ("saw_write", "saw_edit", "saw_bash", "has_write_edit_bash"):
        assert any_write_edit_bash({key: True}) is True
        assert any_write_edit_bash({key: False}) is False


def test_any_write_edit_bash_false():
    assert any_write_edit_bash({"tool_calls": [{"name": "read_file"}]}) is False
    assert any_write_edit_bash({}) is False


def test_any_write_edit_bash_from_conversation_history():
    assert (
        any_write_edit_bash(
            {
                "conversation_history": [
                    {
                        "role": "assistant",
                        "tool_calls": [{"function": {"name": "write_file"}}],
                    },
                    {"role": "user", "content": "hi"},
                ]
            }
        )
        is True
    )
    assert (
        any_write_edit_bash(
            {
                "conversation_history": [
                    {"role": "assistant", "tool_calls": [{"name": "read_file"}]}
                ]
            }
        )
        is False
    )


# ---------- _jev_score (OpenRouter lane) ----------

def test_jev_score_happy_path(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    with patch(f"{MODULE}.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response({"answers": {"q1": {"noul": 0.42}}})
        result = _jev_score(
            user_request="do it",
            final_message="done",
            tool_call_count=1,
            any_write_edit_bash=False,
        )
        assert result == 0.42
        _, kwargs = mock_urlopen.call_args
        assert kwargs.get("timeout") == 4


def test_jev_score_uses_config_model_and_timeout(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    with patch(f"{MODULE}.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response({"answers": {"q1": {"noul": 0.42}}})
        _jev_score(
            user_request="x",
            final_message="y",
            tool_call_count=0,
            any_write_edit_bash=False,
            scorer_model="custom/model:free",
            timeout_s=7,
        )
        _, kwargs = mock_urlopen.call_args
        assert kwargs.get("timeout") == 7
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["model"] == "custom/model:free"


def test_jev_score_clamp_high(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    with patch(f"{MODULE}.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response({"answers": {"q1": {"noul": 1.7}}})
        assert _jev_score() == 1.0


def test_jev_score_clamp_low(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    with patch(f"{MODULE}.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response({"answers": {"q1": {"noul": -0.3}}})
        assert _jev_score() == 0.0


def test_jev_score_bool_raises(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    with patch(f"{MODULE}.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response({"answers": {"q1": {"noul": True}}})
        with pytest.raises(JevScoreError):
            _jev_score()


def test_jev_score_missing_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(JevScoreError):
        _jev_score()


def test_jev_score_url_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    with patch(f"{MODULE}.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("boom")
        with pytest.raises(JevScoreError):
            _jev_score()


def test_jev_score_http_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    with patch(f"{MODULE}.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://x", 500, "err", None, None
        )
        with pytest.raises(JevScoreError):
            _jev_score()


def test_jev_score_bad_json(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    with patch(f"{MODULE}.urllib.request.urlopen") as mock_urlopen:
        resp = MagicMock()
        resp.read.return_value = b"not json"
        mock_urlopen.return_value = resp
        with pytest.raises(JevScoreError):
            _jev_score()


def test_jev_score_missing_answers(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    with patch(f"{MODULE}.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response({})
        with pytest.raises(JevScoreError):
            _jev_score()


def test_jev_score_missing_q1(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    with patch(f"{MODULE}.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response({"answers": {}})
        with pytest.raises(JevScoreError):
            _jev_score()


def test_jev_score_missing_noul(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    with patch(f"{MODULE}.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response({"answers": {"q1": {}}})
        with pytest.raises(JevScoreError):
            _jev_score()


# ---------- Amos lane (mocked HTTP) ----------

def test_amos_score_happy_path(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with patch(f"{MODULE}.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response({"answers": {"q1": {"noul": 0.66}}})
        score = _amos_score("req", "final", 0, False, "medium", 4)
        assert score == 0.66
        _, kwargs = mock_urlopen.call_args
        assert kwargs.get("timeout") == 4
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api.amos.scira.ai/v1/systemone"


def test_amos_score_failure(monkeypatch):
    with patch(f"{MODULE}.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("down")
        with pytest.raises(JevScoreError):
            _amos_score("req", "final", 0, False, "medium", 4)


# ---------- Metrics ----------

def test_metrics_invariant_all_buckets():
    m = Metrics()
    for bucket in ("skipped", "scored", "nudge_injected", "no_nudge", "error"):
        m.record(bucket)
    assert check_metrics_invariant(m) is True
    snap = m.snapshot()
    assert snap["metric_events"] == 5
    assert snap["total_calls"] == 0  # hook-call counter is separate v2 scope
    assert snap["invariant_ok"] is True


def test_metrics_lane_bucket_invariant():
    m = Metrics()
    for bucket in ("dormant", "lane_openrouter", "lane_amos", "lane_failed"):
        m.record(bucket)
    assert check_metrics_invariant(m) is True
    snap = m.snapshot()
    assert snap["metric_events"] == 4
    assert snap["total_calls"] == 0
    assert snap["invariant_ok"] is True


# ---------- on_post_llm_call ----------

def _make_ctx():
    ctx = SimpleNamespace()
    injected = []
    ctx.inject_message = lambda msg: injected.append(msg) or True
    return ctx, injected


def _call(ctx, **kwargs):
    return TurnCompletionNudge(ctx).post_llm_call(**kwargs)


def test_on_post_llm_call_nudge_injected():
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._jev_score", return_value=0.1):
        _call(
            ctx,
            tier="full",
            final_message="I will do next step",
            user_request="do task",
            config={"send_turn_text": True, "enabled": True},  # opt-in explicit (no env defaults)
        )
    assert injected == [_NUDGE_MESSAGE]
    assert mod._METRICS.snapshot()["buckets"]["nudge_injected"] == 1


def test_on_post_llm_call_no_nudge():
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._jev_score", return_value=0.5):
        _call(
            ctx,
            tier="full",
            final_message="I will do next step",
            user_request="do task",
            config={"send_turn_text": True, "enabled": True},
        )
    assert injected == []
    assert mod._METRICS.snapshot()["buckets"]["no_nudge"] == 1


def test_on_post_llm_call_scoring_exception_fail_open():
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._jev_score", side_effect=RuntimeError("boom")), \
         patch(f"{MODULE}._amos_score", side_effect=JevScoreError("amos down")):
        result = _call(
            ctx,
            tier="full",
            final_message="I will do next step",
            user_request="do task",
            config={"send_turn_text": True, "enabled": True, "amos_fallback": True},
        )
    assert result is None
    assert injected == []
    assert mod._METRICS.snapshot()["buckets"]["lane_failed"] == 1


def test_on_post_llm_call_inject_false_records_no_nudge():
    import src.turn_completion_nudge as mod

    ctx = SimpleNamespace()
    ctx.inject_message = lambda msg: False
    handler = TurnCompletionNudge(ctx)
    with patch(f"{MODULE}._jev_score", return_value=0.1):
        handler.post_llm_call(turn_id="turn-1", tier="full", final_message="do next", user_request="task", config={"send_turn_text": True, "enabled": True})
    assert mod._METRICS.snapshot()["buckets"]["no_nudge"] == 1


@pytest.mark.parametrize(
    "override",
    [
        {"tier": "light"},
        {"final_message": ""},
        {"final_message": "Done?"},
        {"user_request": ""},
    ],
)
def test_on_post_llm_call_skip_conditions(override):
    import src.turn_completion_nudge as mod

    ctx, _ = _make_ctx()
    base = {
        "tier": "full",
        "final_message": "Done.",
        "user_request": "task",
    }
    base.update(override)
    with patch(f"{MODULE}._jev_score") as mock_score:
        _call(ctx, **base)
    assert mod._METRICS.snapshot()["buckets"]["skipped"] == 1
    mock_score.assert_not_called()


def test_on_post_llm_call_inject_message_triggers_turn_once_per_turn():
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    handler = TurnCompletionNudge(ctx)
    with patch(f"{MODULE}._jev_score", return_value=0.1):
        handler.post_llm_call(
            turn_id="turn-1",
            tier="full",
            final_message="I will do next step",
            user_request="task first run",
            config={"send_turn_text": True, "enabled": True},
        )
        assert injected == [_NUDGE_MESSAGE]
        assert mod._METRICS.snapshot()["buckets"]["nudge_injected"] == 1

        # repeat same turn_id -> unchanged (guard after eligibility)
        handler.post_llm_call(
            turn_id="turn-1",
            tier="full",
            final_message="I will do next step",
            user_request="task first run",
            config={"send_turn_text": True, "enabled": True},
        )
        assert injected == [_NUDGE_MESSAGE]
        assert mod._METRICS.snapshot()["buckets"]["nudge_injected"] == 1

        # new turn_id -> injected again
        handler.post_llm_call(
            turn_id="turn-2",
            tier="full",
            final_message="I will do next step",
            user_request="task second run",
            config={"send_turn_text": True, "enabled": True},
        )
        assert injected == [_NUDGE_MESSAGE, _NUDGE_MESSAGE]
        assert mod._METRICS.snapshot()["buckets"]["nudge_injected"] == 2


# ---------- WP-1 scorer lane ladder ----------

def test_dormant_when_send_turn_text_false():
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._jev_score") as m_or, patch(f"{MODULE}._amos_score") as m_am:
        _call(
            ctx,
            tier="full",
            final_message="I will do next step",
            user_request="do task",
            config={"send_turn_text": False, "enabled": True},
        )
    assert mod._METRICS.snapshot()["buckets"]["dormant"] == 1
    m_or.assert_not_called()
    m_am.assert_not_called()
    assert injected == []


def test_lane_openrouter_happy():
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._jev_score", return_value=0.15) as m_or:
        _call(
            ctx,
            tier="full",
            final_message="I will do next step",
            user_request="do task",
            config={"send_turn_text": True, "enabled": True},
        )
    assert mod._METRICS.snapshot()["buckets"]["lane_openrouter"] == 1
    m_or.assert_called_once()
    assert injected == [_NUDGE_MESSAGE]


def test_openrouter_score_logs_served_model(monkeypatch):
    import src.turn_completion_nudge as mod

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    with patch(f"{MODULE}.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response({"answers": {"q1": {"noul": 0.42}}})
        score = mod._openrouter_score("req", "final", 0, False, "custom/model:free", 4)
        assert score == 0.42
        assert mod._SERVED_MODEL == "custom/model:free"


def test_lane_amos_fallback_on_openrouter_error():
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._jev_score", side_effect=JevScoreError("no key")), \
         patch(f"{MODULE}._amos_score", return_value=0.3) as m_am:
        _call(
            ctx,
            tier="full",
            final_message="I will do next step",
            user_request="do task",
            config={"send_turn_text": True, "enabled": True, "amos_fallback": True},  # explicit second opt-in
        )
    assert mod._METRICS.snapshot()["buckets"]["lane_amos"] == 1
    m_am.assert_called_once()
    assert injected == []


def test_lane_amos_fallback_on_missing_key():
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._jev_score", side_effect=JevScoreError("missing OPENROUTER_API_KEY")), \
         patch(f"{MODULE}._amos_score", return_value=0.4) as m_am:
        _call(
            ctx,
            tier="full",
            final_message="I will do next step",
            user_request="do task",
            config={"send_turn_text": True, "enabled": True, "amos_fallback": True},
        )
    assert mod._METRICS.snapshot()["buckets"]["lane_amos"] == 1
    m_am.assert_called_once()


def test_lane_failed_on_both_fail():
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._jev_score", side_effect=JevScoreError("or down")), \
         patch(f"{MODULE}._amos_score", side_effect=JevScoreError("amos down")):
        result = _call(
            ctx,
            tier="full",
            final_message="I will do next step",
            user_request="do task",
            config={"send_turn_text": True, "enabled": True},
        )
    assert result is None
    assert injected == []
    assert mod._METRICS.snapshot()["buckets"]["lane_failed"] == 1


def test_config_live_read_send_turn_text_toggle():
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    # opted out -> dormant, no scoring
    with patch(f"{MODULE}._jev_score") as m_or:
        _call(
            ctx,
            tier="full",
            final_message="I will do next step",
            user_request="do task",
            config={"send_turn_text": False, "enabled": True},
        )
    assert mod._METRICS.snapshot()["buckets"]["dormant"] == 1
    m_or.assert_not_called()

    # opted in -> scoring runs (live config read per call)
    ctx2, injected2 = _make_ctx()
    with patch(f"{MODULE}._jev_score", return_value=0.5) as m_or:
        _call(
            ctx2,
            tier="full",
            final_message="I will do next step",
            user_request="do task",
            config={"send_turn_text": True, "enabled": True},
        )
    assert mod._METRICS.snapshot()["buckets"]["lane_openrouter"] == 1
    m_or.assert_called_once()


def test_plugin_writes_nothing_under_plugin_data(tmp_path):
    import src.turn_completion_nudge as mod

    data_dir = tmp_path / "plugin-data"
    data_dir.mkdir()
    before = set(os.listdir(data_dir))
    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._jev_score", return_value=0.5):
        _call(
            ctx,
            tier="full",
            final_message="I will do next step",
            user_request="do task",
            config={"send_turn_text": True, "enabled": True},
        )
    after = set(os.listdir(data_dir))
    assert before == after


def test_guard_cap_1024_drop_oldest():
    import src.turn_completion_nudge as mod

    assert mod._MAX_NUDGED_TURN_IDS == 1024
    handler = mod.TurnCompletionNudge(_make_ctx()[0])
    for i in range(1024 + 10):
        handler._guard_turn(f"turn-{i}")
    assert len(handler._nudged_ids) <= 1024
    assert "turn-0" not in handler._nudged_ids
    assert "turn-1033" in handler._nudged_ids


# ---------- contract tests (WP-5a acceptance 1) ----------

def test_contract_injects_nudge_exact_fire_site_payload_captured_ctx():
    """(a) Exact fire-site payload shape (assistant_response / user_message /
    turn_id / conversation_history / model / platform / session_id / task_id /
    tier; no final_message / user_request / ctx keys) with _jev_score patched
    to 0.1 and a captured ctx asserts the nudge message is injected.
    """
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    ctx.get_config = lambda key: {"enabled": True, "send_turn_text": True}.get(key)
    handler = TurnCompletionNudge(ctx)
    with patch(f"{MODULE}._jev_score", return_value=0.1) as mock_score:
        handler.post_llm_call(
            assistant_response="I will do next step",
            user_message="do task",
            turn_id="turn-1",
            conversation_history=[
                {"role": "assistant", "content": "I will do next step"},
                {
                    "role": "assistant",
                    "tool_calls": [{"name": "write_file", "arguments": {"path": "x"}}],
                },
            ],
            model="some-model",
            platform="cli",
            session_id="sess-1",
            task_id="task-1",
            tier="full",
        )
    assert injected == [_NUDGE_MESSAGE]
    assert mod._METRICS.snapshot()["buckets"]["nudge_injected"] == 1
    mock_score.assert_called_once()
    _, score_kwargs = mock_score.call_args
    assert score_kwargs["tool_call_count"] == 1
    assert score_kwargs["any_write_edit_bash"] is True


def test_contract_scorer_still_runs_without_ctx_records_no_nudge():
    """(b) Same fire-site shape with ctx absent (unit-level) but explicit
    opted-in config: scorer path still runs and records no_nudge, documenting
    the real dispatch contract where the dispatcher never injects ctx into
    hook callbacks (standalone opt-in arrives as kwargs config).
    """
    import src.turn_completion_nudge as mod

    handler = TurnCompletionNudge(None)
    with patch(f"{MODULE}._jev_score", return_value=0.1) as mock_score:
        handler.post_llm_call(
            assistant_response="I will do next step",
            user_message="do task",
            turn_id="turn-1",
            conversation_history=[
                {"role": "assistant", "tool_calls": [{"name": "write_file"}]},
            ],
            model="some-model",
            platform="cli",
            session_id="sess-1",
            task_id="task-1",
            tier="full",
            config={"send_turn_text": True, "enabled": True},
        )
    assert mod._METRICS.snapshot()["buckets"]["no_nudge"] == 1
    mock_score.assert_called_once()


# ---------- CLI status (process-local) ----------

def test_status_cli_output(capsys):
    import src.turn_completion_nudge as mod

    mod._LAST_LANE_CONFIG.update(
        {
            "enabled": True,
            "send_turn_text": True,
            "threshold": 0.2,
            "scorer_model": "m:free",
            "amos_model": "medium",
            "timeout_s": 4,
        }
    )
    mod._METRICS.record_score(0.15)

    class Args:
        json = False

    mod._status_cli(Args())
    captured = capsys.readouterr()
    assert "turn-completion-nudge status" in captured.out
    assert "process-local" in captured.out
    assert "opted_in (send_turn_text): True" in captured.out
    assert "last_scores (process-local): [0.15]" in captured.out
    assert "metric_events:" in captured.out
    assert "legacy_metric_events:" in captured.out


def test_status_cli_json_fields_and_scopes(capsys):
    import src.turn_completion_nudge as mod

    mod._LAST_LANE_CONFIG.update(
        {
            "enabled": True,
            "send_turn_text": False,
            "threshold": 0.2,
            "scorer_model": "m:free",
            "amos_model": "medium",
            "timeout_s": 4,
        }
    )
    mod._METRICS.record_score(0.33)
    mod._METRICS.record("skipped")
    mod._METRICS.record_hook_call()

    class Args:
        json = True

    mod._status_cli(Args())
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == 2
    assert out["lane_config_process_local"] is True
    assert out["last_scores_scope"] == "process_local"
    assert out["last_scores"] == [0.33]
    assert out["total_calls"] == 1
    assert out["total_calls_scope"] == "enabled_post_llm_call_invocations_v2"
    assert out["total_calls_epoch"] == "v2"
    assert out["metric_events"] == 1
    assert out["legacy_metric_events"] == 0
    assert out["invariant_ok"] is True
    assert "process_local" not in out  # renamed to lane_config_process_local


# ---------- placeholder key validation ----------

def test_validate_openrouter_api_key_messages():
    import src.turn_completion_nudge as mod

    for bad in (None, " ", "REPLACE_WITH_KEY", "your-key-here", "sk-or-..."):
        msg = mod._validate_openrouter_api_key(bad)
        assert isinstance(msg, str) and msg
        assert "OPENROUTER_API_KEY" in msg
    assert mod._validate_openrouter_api_key("sk-or-liveabc123") is None


def test_openrouter_placeholder_key_warns_once(monkeypatch, caplog):
    import src.turn_completion_nudge as mod

    monkeypatch.setenv("OPENROUTER_API_KEY", "REPLACE_WITH_KEY")
    mod._PLACEHOLDER_KEY_WARNED = False
    with caplog.at_level(logging.WARNING):
        with pytest.raises(JevScoreError):
            mod._openrouter_score("req", "final", 0, False, "m", 4)
        first = sum("OPENROUTER_API_KEY" in r.getMessage() for r in caplog.records)
        assert first == 1
        with pytest.raises(JevScoreError):
            mod._openrouter_score("req", "final", 0, False, "m", 4)
        second = sum("OPENROUTER_API_KEY" in r.getMessage() for r in caplog.records)
        assert second == 1


def test_openrouter_real_key_no_warning_proceeds(monkeypatch, caplog):
    import src.turn_completion_nudge as mod

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-liveabc123")
    mod._PLACEHOLDER_KEY_WARNED = False
    with caplog.at_level(logging.WARNING):
        with patch(f"{MODULE}.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response({"answers": {"q1": {"noul": 0.4}}})
            result = mod._openrouter_score("req", "final", 0, False, "m", 4)
    assert result == 0.4
    assert not any("OPENROUTER_API_KEY" in r.getMessage() for r in caplog.records)


def test_lane_amos_fallback_on_placeholder_key(monkeypatch):
    import src.turn_completion_nudge as mod

    monkeypatch.setenv("OPENROUTER_API_KEY", "REPLACE_WITH_KEY")
    mod._PLACEHOLDER_KEY_WARNED = False
    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._amos_score", return_value=0.3) as m_am:
        _call(
            ctx,
            tier="full",
            final_message="I will do next step",
            user_request="do task",
            config={"send_turn_text": True, "enabled": True, "amos_fallback": True},
        )
    assert mod._METRICS.snapshot()["buckets"]["lane_amos"] == 1
    m_am.assert_called_once()


def test_production_default_absent_key_dormant(monkeypatch):
    """Default install semantics: absent send_turn_text -> dormant, no scorer, no egress.

    The exact scenario the WP-6 review blocked on. There is no pytest env bridge:
    absent means dormant in EVERY context, so this runs identically under pytest
    and production (the WP-r2 fix, restated by the upstream 2026-11-09 review).
    """
    import src.turn_completion_nudge as mod
    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._jev_score") as m_or, patch(f"{MODULE}._amos_score") as m_am:
        _call(
            ctx,
            tier="full",
            final_message="I will do next step",
            user_request="do task",
        )
    assert mod._METRICS.snapshot()["buckets"]["dormant"] == 1
    m_or.assert_not_called()
    m_am.assert_not_called()
    assert injected == []


def test_string_false_send_turn_text_dormant():
    """Config send_turn_text="false" (string) must be dormant too (strict bool parse guard)."""
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._jev_score") as m_or, patch(f"{MODULE}._amos_score") as m_am:
        _call(
            ctx,
            tier="full",
            final_message="I will do next step",
            user_request="do task",
            config={"send_turn_text": "false", "enabled": True},
        )
    assert mod._METRICS.snapshot()["buckets"]["dormant"] == 1
    m_or.assert_not_called()
    m_am.assert_not_called()
    assert injected == []


# ---------- upstream 2026-11-10 review round: amos_fallback opt-in + nudge cap + secret scope ----------

def test_amos_fallback_defaults_off_lane_failed():
    """OpenRouter failure without optsk-in must NOT silently egress to Amos/Scira."""
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._jev_score", side_effect=JevScoreError("no key")), \
         patch(f"{MODULE}._amos_score") as m_am:
        result = _call(
            ctx,
            tier="full",
            final_message="I will do next step",
            user_request="do task",
            config={"send_turn_text": True, "enabled": True},
        )
    assert result is None
    assert mod._METRICS.snapshot()["buckets"]["lane_failed"] == 1
    m_am.assert_not_called()
    assert injected == []


def test_lane_config_amos_keys():
    """amos_fallback defaults false; string 'false' stays false; base URL default + override."""
    import src.turn_completion_nudge as mod

    defaults = mod._read_lane_config(None, {})
    assert defaults["amos_fallback"] is False
    assert defaults["amos_base_url"] == "https://api.amos.scira.ai/v1/systemone"
    strict_false = mod._read_lane_config(None, {"config": {"amos_fallback": "false"}})
    assert strict_false["amos_fallback"] is False
    on = mod._read_lane_config(None, {"config": {"amos_fallback": True, "amos_base_url": "http://127.0.0.1:9/v1/systemone"}})
    assert on["amos_fallback"] is True
    assert on["amos_base_url"] == "http://127.0.0.1:9/v1/systemone"


def test_amos_score_uses_config_base_url():
    """The amos lane POSTs to the amos_base_url passed by config, not a frozen env constant."""
    import src.turn_completion_nudge as mod

    with patch(f"{MODULE}._post_json", return_value={"answers": {"q1": {"noul": 0.6}}}) as m_post:
        score = mod._amos_score("req", "final", 0, False, "medium", 1, amos_base_url="http://127.0.0.1:9/v1/systemone")
    assert score == 0.6
    assert m_post.call_args[0][0] == "http://127.0.0.1:9/v1/systemone"


def test_read_openrouter_key_env_fallback(monkeypatch):
    """Without agent.secret_scope the declared env var is used (standalone path)."""
    import src.turn_completion_nudge as mod

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert mod._read_openrouter_key() is None
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    assert mod._read_openrouter_key() == "env-key"


def test_secret_scope_key_preferred_over_env(monkeypatch):
    """agent.secret_scope.get_secret wins over the env var (profile-scoped secrets)."""
    import sys, types
    import src.turn_completion_nudge as mod

    pkg = types.ModuleType("agent")
    sub = types.ModuleType("agent.secret_scope")
    sub.get_secret = lambda name: {"OPENROUTER_API_KEY": "scope-key"}.get(name)
    monkeypatch.setitem(sys.modules, "agent", pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", sub)
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    assert mod._read_openrouter_key() == "scope-key"


def test_secret_scope_empty_value_returned_as_is(monkeypatch):
    """A scope refusal (None) is returned as-is — never bypassed via env re-read.

    get_secret refuses only when one process serves several profiles and this
    profile has no key of its own; re-reading the env there would bill the
    launch profile's key for this profile's traffic (upstream review round 2).
    """
    import sys, types
    import src.turn_completion_nudge as mod

    pkg = types.ModuleType("agent")
    sub = types.ModuleType("agent.secret_scope")
    sub.get_secret = lambda name: None
    monkeypatch.setitem(sys.modules, "agent", pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", sub)
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    assert mod._read_openrouter_key() is None


def test_secret_scope_error_propagates_no_env_fallback(monkeypatch):
    """get_secret failures are not swallowed into an env re-read (as-is contract)."""
    import sys, types
    import src.turn_completion_nudge as mod

    pkg = types.ModuleType("agent")
    sub = types.ModuleType("agent.secret_scope")

    def _boom(name):
        raise RuntimeError("scope backend down")

    sub.get_secret = _boom
    monkeypatch.setitem(sys.modules, "agent", pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", sub)
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    with pytest.raises(RuntimeError):
        mod._read_openrouter_key()


def test_consecutive_nudge_capped_after_two_then_reset_by_new_user_turn():
    """A chain of nudge-driven turns nudges at most _MAX_CONSECUTIVE_NUDGES times.

    Uses ONE persistent handler (as production does: one instance built at
    register time), since the chain depth lives on the instance.
    """
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    import logging, io
    stream = io.StringIO()
    handler_log = logging.StreamHandler(stream)
    mod.logger.addHandler(handler_log)
    old_level = mod.logger.level
    mod.logger.setLevel(logging.INFO)  # pytest root WARNING otherwise swallows the cap INFO line
    handler = TurnCompletionNudge(ctx)
    try:
        with patch(f"{MODULE}._jev_score", return_value=0.1):
            handler.post_llm_call(
                tier="full",
                final_message="I will do next step",
                user_request="real user turn",
                config={"send_turn_text": True, "enabled": True},
            )  # nudge #1 (chain starts)
            handler.post_llm_call(
                tier="full",
                final_message="I will do next step",
                user_request=mod._NUDGE_MESSAGE,
                config={"send_turn_text": True, "enabled": True},
            )  # nudge #2 (chain hop 1)
            handler.post_llm_call(
                tier="full",
                final_message="I will do next step",
                user_request=mod._NUDGE_MESSAGE,
                config={"send_turn_text": True, "enabled": True},
            )  # blocked (cap reached)
    finally:
        mod.logger.removeHandler(handler_log)
        mod.logger.setLevel(old_level)
    assert len([m for m in injected if m == mod._NUDGE_MESSAGE]) == 2
    assert mod._METRICS.snapshot()["buckets"]["nudge_injected"] == 2
    assert "consecutive-nudge cap" in stream.getvalue()


def test_consecutive_nudge_reset_on_new_user_turn_and_on_complete(monkeypatch):
    """Chain depth resets when a real user turn arrives or a turn scores complete.

    One persistent handler (production shape) — otherwise depth would reset
    spuriously per call and this would pass vacuously.
    """
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    handler = TurnCompletionNudge(ctx)
    with patch(f"{MODULE}._jev_score", return_value=0.1):
        handler.post_llm_call(tier="full", final_message="mid", user_request="u1",
              config={"send_turn_text": True, "enabled": True})  # nudge, depth 1
        handler.post_llm_call(tier="full", final_message="mid", user_request=mod._NUDGE_MESSAGE,
              config={"send_turn_text": True, "enabled": True})  # chain hop, depth 2
    with patch(f"{MODULE}._jev_score", return_value=0.9):
        handler.post_llm_call(tier="full", final_message="complete turn", user_request="u2",
              config={"send_turn_text": True, "enabled": True})  # complete -> depth 0
    with patch(f"{MODULE}._jev_score", return_value=0.1):
        handler.post_llm_call(tier="full", final_message="mid", user_request=mod._NUDGE_MESSAGE,
              config={"send_turn_text": True, "enabled": True})  # allowed again (depth reset)
    assert mod._METRICS.snapshot()["buckets"]["nudge_injected"] == 3


# ---------- metrics persistence ----------


def test_metrics_persist_round_trip(tmp_path, monkeypatch):
    import src.turn_completion_nudge as mod

    state_file = tmp_path / "state" / "metrics.json"
    monkeypatch.setattr(mod, "_state_path", lambda: str(state_file))

    m1 = Metrics()
    m1.record_hook_call()
    m1.record("scored")
    m1.record("nudge_injected")
    m1.record("no_nudge")

    m2 = Metrics()
    s1 = m1.snapshot()
    s2 = m2.snapshot()
    assert s2["total_calls"] == s1["total_calls"] == 1
    assert s2["metric_events"] == s1["metric_events"] == 3
    assert s2["buckets"] == s1["buckets"]
    assert s2["invariant_ok"] is True

    loaded = mod._load_persisted_snapshot()
    assert loaded is not None
    assert loaded["schema_version"] == 2
    assert loaded["total_calls"] == 1
    assert loaded["metric_events"] == 3
    assert loaded["legacy_metric_events"] == 0
    assert loaded["buckets"] == s1["buckets"]
    assert loaded["invariant_ok"] is True
    assert loaded["count_scope"] == "enabled_post_llm_call_invocations_v2"


def test_metrics_corrupt_json_fallback(tmp_path, monkeypatch):
    import src.turn_completion_nudge as mod

    state_file = tmp_path / "state" / "metrics.json"
    monkeypatch.setattr(mod, "_state_path", lambda: str(state_file))
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{not valid json", encoding="utf-8")

    # Loading must not raise and must fall back (None).
    assert mod._load_persisted_snapshot() is None

    # Constructing Metrics must not raise and starts from zeros.
    m = Metrics()
    snap = m.snapshot()
    assert snap["total_calls"] == 0
    assert snap["metric_events"] == 0
    assert all(v == 0 for v in snap["buckets"].values())
    assert snap["invariant_ok"] is True

    # record() still works.
    m.record("skipped")
    assert m.snapshot()["metric_events"] == 1
    assert m.snapshot()["total_calls"] == 0

    # status-cli-equivalent load falls back to process snapshot, source: process.
    # m.record() persisted a valid snapshot; restore corrupt file to force fallback.
    state_file.write_text("{not valid json", encoding="utf-8")
    class Args:
        json = True

    from io import StringIO
    import sys

    buf = StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        mod._status_cli(Args())
    finally:
        sys.stdout = old
    out = json.loads(buf.getvalue())
    assert out["source"] == "process"
    assert out["total_calls"] == 0
    assert out["metric_events"] == 0
    assert out["invariant_ok"] is True


def test_metrics_write_failure_record_ok(tmp_path, monkeypatch):
    import src.turn_completion_nudge as mod

    state_file = tmp_path / "state" / "metrics.json"
    monkeypatch.setattr(mod, "_state_path", lambda: str(state_file))

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)

    m = Metrics()
    # Should not raise despite persistence failure.
    m.record_hook_call()
    m.record("error")

    snap = m.snapshot()
    assert snap["total_calls"] == 1
    assert snap["metric_events"] == 1
    assert snap["buckets"]["error"] == 1
    assert snap["invariant_ok"] is True
    assert mod.check_metrics_invariant(m) is True


# ---------- required audit regression coverage ----------


def _hook_kwargs(**overrides):
    base = {
        "tier": "full",
        "final_message": "I will do next step",
        "user_request": "do task",
        "config": {"send_turn_text": True, "enabled": True},
    }
    base.update(overrides)
    return base


def test_hook_call_accounting_success_skip_dormant_lane_failed_outer_error():
    import src.turn_completion_nudge as mod

    # success/nudge path (enabled hook call)
    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._jev_score", return_value=0.1):
        _call(ctx, **_hook_kwargs())
    assert injected == [_NUDGE_MESSAGE]

    # skip path
    ctx2, _ = _make_ctx()
    with patch(f"{MODULE}._jev_score") as mock_score:
        _call(ctx2, **_hook_kwargs(tier="light"))
    mock_score.assert_not_called()

    # dormant path
    ctx3, _ = _make_ctx()
    with patch(f"{MODULE}._jev_score") as mock_score:
        _call(ctx3, **_hook_kwargs(config={"send_turn_text": False, "enabled": True}))
    mock_score.assert_not_called()

    # lane failure path (OpenRouter fails, no amos fallback)
    ctx4, _ = _make_ctx()
    with patch(f"{MODULE}._jev_score", side_effect=JevScoreError("boom")):
        _call(ctx4, **_hook_kwargs(config={"send_turn_text": True, "enabled": True, "amos_fallback": False}))

    # outer error path: force an unexpected exception inside hook after counting.
    ctx5, _ = _make_ctx()
    with patch(f"{MODULE}._read_lane_config", side_effect=RuntimeError("kaboom")):
        result = _call(ctx5, **_hook_kwargs())
    assert result is None

    snap = mod._METRICS.snapshot()
    assert snap["total_calls"] == 5
    assert snap["buckets"]["lane_openrouter"] == 1
    assert snap["buckets"]["nudge_injected"] == 1
    assert snap["buckets"]["skipped"] == 1
    assert snap["buckets"]["dormant"] == 1
    assert snap["buckets"]["lane_failed"] == 1
    assert snap["buckets"]["error"] == 1
    assert snap["metric_events"] == 6
    assert snap["invariant_ok"] is True

    loaded = mod._load_persisted_snapshot()
    assert loaded is not None
    assert loaded["schema_version"] == 2
    assert loaded["total_calls"] == 5
    assert loaded["metric_events"] == 6
    assert loaded["invariant_ok"] is True


def test_disabled_hook_is_true_noop_not_counted():
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._jev_score") as mock_score:
        result = _call(ctx, **_hook_kwargs(config={"enabled": False, "send_turn_text": True}))
    assert result is None
    assert injected == []
    mock_score.assert_not_called()
    snap = mod._METRICS.snapshot()
    assert snap["total_calls"] == 0
    assert snap["metric_events"] == 0
    assert all(v == 0 for v in snap["buckets"].values())


def test_v1_to_v2_migration_preserves_legacy_events_no_fabricated_calls(tmp_path, monkeypatch):
    import src.turn_completion_nudge as mod

    state_file = tmp_path / "state" / "metrics.json"
    monkeypatch.setattr(mod, "_state_path", lambda: str(state_file))
    monkeypatch.setattr(mod, "_LAST_PERSISTED_SEQ", 0)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    v1 = {
        "total_calls": 7,
        "buckets": {"skipped": 2, "scored": 0, "nudge_injected": 1, "no_nudge": 3, "error": 1},
        "invariant_ok": True,
        "last_updated": "2026-11-24T00:00:00+00:00",
    }
    state_file.write_text(json.dumps(v1), encoding="utf-8")

    loaded = mod._load_persisted_snapshot()
    assert loaded is not None
    assert loaded["schema_version"] == 2
    assert loaded["metric_events"] == 7
    assert loaded["legacy_metric_events"] == 7
    assert loaded["total_calls"] == 0
    assert loaded["count_scope"] == "enabled_post_llm_call_invocations_v2"
    assert loaded["count_epoch"] == "migrated_from_v1"
    assert loaded["buckets"]["skipped"] == 2
    assert loaded["buckets"]["nudge_injected"] == 1
    assert loaded["buckets"]["no_nudge"] == 3
    assert loaded["buckets"]["error"] == 1
    assert loaded["invariant_ok"] is True

    m = Metrics()
    snap = m.snapshot()
    assert snap["total_calls"] == 0  # no fabricated historical hook-call counts
    assert snap["metric_events"] == 7
    assert snap["legacy_metric_events"] == 7
    assert snap["count_epoch"] == "migrated_from_v1"
    assert snap["invariant_ok"] is True


def test_invalid_threshold_and_timeout_fallback_warn_and_default(caplog):
    import src.turn_completion_nudge as mod

    bad_thresholds = [float("nan"), float("inf"), -0.1, 1.1, True, False, "0.5", "bad"]
    for bad in bad_thresholds:
        with caplog.at_level(logging.WARNING):
            cfg = mod._read_lane_config(None, {"config": {"threshold": bad, "timeout_s": 4}})
        assert cfg["threshold"] == 0.2, bad
        assert cfg["timeout_s"] == 4

    bad_timeouts = [0, -1, True, False, 1.5, "4.0", "abc", None]
    for bad in bad_timeouts:
        with caplog.at_level(logging.WARNING):
            cfg = mod._read_lane_config(None, {"config": {"threshold": 0.2, "timeout_s": bad}})
        assert cfg["timeout_s"] == 4, bad
        assert cfg["threshold"] == 0.2

    # valid edge cases
    assert mod._read_lane_config(None, {"config": {"threshold": 0.0}})["threshold"] == 0.0
    assert mod._read_lane_config(None, {"config": {"threshold": 1.0}})["threshold"] == 1.0
    assert mod._read_lane_config(None, {"config": {"timeout_s": 1}})["timeout_s"] == 1
    assert mod._read_lane_config(None, {"config": {"timeout_s": "7"}})["timeout_s"] == 7


def test_invalid_config_values_fail_open_no_egress():
    import src.turn_completion_nudge as mod

    ctx, injected = _make_ctx()
    with patch(f"{MODULE}._jev_score", return_value=0.1) as mock_score:
        _call(
            ctx,
            **_hook_kwargs(
                config={
                    "send_turn_text": True,
                    "enabled": True,
                    "threshold": float("nan"),
                    "timeout_s": 0,
                }
            ),
        )
    # Invalid config falls back to safe defaults; threshold default 0.2 means 0.1 still nudges.
    assert injected == [_NUDGE_MESSAGE]
    mock_score.assert_called_once()
    _, kwargs = mock_score.call_args
    assert kwargs["timeout_s"] == 4
    snap = mod._METRICS.snapshot()
    assert snap["total_calls"] == 1
    assert snap["metric_events"] == 2  # lane_openrouter + nudge_injected
    assert snap["buckets"]["nudge_injected"] == 1


def test_forced_out_of_order_concurrent_writes_final_persisted_matches_memory(tmp_path, monkeypatch):
    """Deterministic race: force older snapshot commit attempt after newer commit.

    Final persisted snapshot must equal the in-memory state despite out-of-order
    writer scheduling. Asserts persisted JSON fields and schema, not only return values.
    """
    import src.turn_completion_nudge as mod

    state_file = tmp_path / "state" / "metrics.json"
    monkeypatch.setattr(mod, "_state_path", lambda: str(state_file))
    monkeypatch.setattr(mod, "_LAST_PERSISTED_SEQ", 0)

    m = Metrics()
    real_persist = mod._persist_snapshot
    first_before_commit = threading.Event()
    allow_first_commit = threading.Event()
    second_committed = threading.Event()
    state = {"first_seen": False}

    def wrapped_persist(snapshot):
        seq = int(snapshot.get("snapshot_seq", 0))
        if seq == 1 and not state["first_seen"]:
            state["first_seen"] = True
            first_before_commit.set()
            assert allow_first_commit.wait(timeout=5)
        real_persist(snapshot)
        if seq == 2:
            second_committed.set()

    monkeypatch.setattr(mod, "_persist_snapshot", wrapped_persist)

    # seq1: older writer starts, pauses before commit.
    t1 = threading.Thread(target=lambda: m.record("skipped"), name="older-writer")
    t1.start()
    assert first_before_commit.wait(timeout=5)

    # seq2: newer writer commits while older is paused.
    t2 = threading.Thread(target=lambda: m.record("scored"), name="newer-writer")
    t2.start()
    assert second_committed.wait(timeout=5)

    # Allow older writer to attempt commit; seq guard must reject it.
    allow_first_commit.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive()
    assert not t2.is_alive()

    snap = m.snapshot()
    assert snap["metric_events"] == 2
    assert snap["total_calls"] == 0
    assert snap["buckets"]["skipped"] == 1
    assert snap["buckets"]["scored"] == 1
    assert snap["invariant_ok"] is True

    raw = json.loads(state_file.read_text(encoding="utf-8"))
    # Assert persisted JSON fields and schema, not only return values.
    assert raw["schema_version"] == 2
    assert raw["total_calls"] == snap["total_calls"]
    assert raw["metric_events"] == snap["metric_events"]
    assert raw["legacy_metric_events"] == snap["legacy_metric_events"]
    assert raw["buckets"] == snap["buckets"]
    assert raw["invariant_ok"] is True
    assert raw["count_scope"] == snap["count_scope"]
    assert raw["count_epoch"] == snap["count_epoch"]
    assert raw["snapshot_seq"] == snap["snapshot_seq"]
    assert isinstance(raw["last_updated"], str) and raw["last_updated"]

    # Final persisted snapshot equals in-memory state.
    loaded = mod._load_persisted_snapshot()
    assert loaded is not None
    for key in ("schema_version", "total_calls", "metric_events", "legacy_metric_events",
                "buckets", "invariant_ok", "count_scope", "count_epoch", "snapshot_seq"):
        assert loaded[key] == snap[key]
