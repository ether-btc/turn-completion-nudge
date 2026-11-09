#!/usr/bin/env python3
"""Pi5 probe: one real scoring round-trip for both scorer lanes.

Stdlib only (urllib, json, threading, time). Synthetic (non-conversation)
payloads only. Measures latency of one OpenRouter lane call and one Amos
systemone lane call, writes medians into docs/pi5_evidence.json.

This is a probe, not a test: it attempts real HTTP and records the honest
outcome (including "no key" / "network unavailable"), never fabricates.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
AMOS_URL = "https://api.amos.scira.ai/v1/systemone"

SCORER_MODEL = os.environ.get("TCN_SCORER_MODEL", "respan/span-01-lite-20260925:free")
AMOS_MODEL = os.environ.get("TCN_AMOS_MODEL", "medium")
TIMEOUT_S = int(os.environ.get("TCN_TIMEOUT_S", "4"))

# Synthetic (non-conversation) scorer state: clipped text only.
SYNTHETIC_STATE = (
    "User request: synthetic probe request (turn-completion scoring latency).\n"
    "Final message: synthetic probe final message acknowledging the request.\n"
    "Tool call count: 0\n"
    "Has write/edit/bash: False\n"
)

NOUL_QUESTION = {
    "type": "noul",
    "instructions": "Score whether the assistant final message completes the user request.",
    "criteria": {
        "true": "The turn is complete; no further action is needed.",
        "false": "The turn is incomplete; the agent should continue.",
    },
}


def _post_once(url, body, headers, timeout_s):
    """POST once; return (latency_s, parsed_payload_or_error_dict)."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
        latency = time.monotonic() - start
        try:
            return latency, json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - record honestly
            return latency, {"_parse_error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - probe records honestly
        return None, {"_error": f"{type(exc).__name__}: {exc}"}


def _noul_body(model):
    return {"model": model, "state": SYNTHETIC_STATE, "questions": {"q1": NOUL_QUESTION}}


def main():
    results = {
        "probe": "turn-completion-nudge scoring round-trip",
        "payload": "synthetic (non-conversation)",
        "timeout_s": TIMEOUT_S,
        "lanes": {},
    }

    # OpenRouter lane (key only from declared env var; never reads .env files).
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        results["lanes"]["openrouter"] = {
            "model": SCORER_MODEL,
            "skipped": "OPENROUTER_API_KEY not set",
        }
    else:
        latency, out = _post_once(
            OPENROUTER_URL,
            _noul_body(SCORER_MODEL),
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            TIMEOUT_S,
        )
        results["lanes"]["openrouter"] = {
            "model": SCORER_MODEL,
            "latency_s": latency,
            "ok": latency is not None and "_error" not in (out or {}),
            "served_model": (out or {}).get("model") if isinstance(out, dict) else None,
            "error": None if latency is not None else (out or {}).get("_error"),
        }

    # Amos lane (keyless fallback, one hop).
    latency, out = _post_once(
        AMOS_URL, _noul_body(AMOS_MODEL), {"Content-Type": "application/json"}, TIMEOUT_S
    )
    amos_out = out or {}
    results["lanes"]["amos"] = {
        "model": AMOS_MODEL,
        "latency_s": latency,
        "ok": latency is not None and "_error" not in amos_out,
        "served_model": amos_out.get("model") if isinstance(amos_out, dict) else None,
        "error": None if latency is not None else amos_out.get("_error"),
    }

    latencies = [
        lane["latency_s"]
        for lane in results["lanes"].values()
        if isinstance(lane.get("latency_s"), (int, float))
    ]
    results["medians_seconds"] = {
        "openrouter": results["lanes"].get("openrouter", {}).get("latency_s"),
        "amos": results["lanes"].get("amos", {}).get("latency_s"),
        "total": (sum(latencies) / len(latencies)) if latencies else None,
    }
    results["assert_under_5s"] = bool(latencies) and sum(latencies) < 5.0

    # Write evidence JSON under the plugin's docs/.
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, os.pardir))
    docs_dir = os.path.join(repo_root, "docs")
    os.makedirs(docs_dir, exist_ok=True)
    evidence_path = os.path.join(docs_dir, "pi5_evidence.json")
    with open(evidence_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
