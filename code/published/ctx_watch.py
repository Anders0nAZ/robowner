"""Did a model call overflow its context window? Record every call and say so.

Ollama does not tell you. Measured on 0.33.1, 23 Sep 2026, against the 48k tag:
an over-long prompt comes back HTTP 200, no error, `done_reason: stop`, with
the front of the prompt -- the system prompt -- silently cut away. One
oversized message is cut to half the window; a multi-message prompt loses its
oldest messages. The reported `prompt_eval_count` then looks innocent (24,578
for a ~70k-token prompt), so "is the count near the window?" misses it.

What does not lie is characters sent per token counted. Real prompts run 2.4
(the responder's JSON-heavy prompt, tool schemas included) to 5.4 (plain
prose); both truncated calls measured 10-15, because characters were sent that
were never counted. TRUNCATED_CHARS_PER_TOKEN sits well clear of both.

This module only WATCHES. It changes no prompt and no output: it exists to
answer whether overflow happens often enough -- or ever -- to justify going
back to the 96k tag.

    python -m robo.ctx_watch          # the last week, per caller
"""

from __future__ import annotations

import json
import time
from collections import defaultdict

from robo import DATA

LOG = DATA / "ctx_usage.jsonl"
# Characters per counted token above which the prompt cannot have been read in
# full. Legitimate prompts measured 2.4-5.4; truncated ones 10-15.
TRUNCATED_CHARS_PER_TOKEN = 8.0
# Prompt plus output past this share of the window is worth knowing about
# before it becomes an overflow. Thinking tokens count against the window too.
NEAR_SHARE = 0.70
KEEP_ROWS = 20_000

# The window each tag has baked in; a call to an unlisted tag is still logged
# and still checked for truncation, just not for nearness.
WINDOWS = {"qwen3.8:27b-mtp-48k-text": 49152, "qwen3.8:27b-mtp-96k": 98304}


def _chars(messages, tools=None) -> int:
    n = sum(len(m.get("content") or "") for m in messages or [])
    # Tool definitions are templated into the prompt and counted as tokens, so
    # they belong in the ratio or the responder would read as over-dense.
    if tools:
        n += len(json.dumps(tools))
    return n


def verdict(chars: int, prompt_tokens: int | None, total_tokens: int | None,
            window: int | None) -> str:
    if not prompt_tokens:
        return "unknown"
    if chars / prompt_tokens > TRUNCATED_CHARS_PER_TOKEN:
        return "truncated"
    if window and total_tokens and total_tokens > NEAR_SHARE * window:
        return "near"
    return "ok"


def record(caller: str, model: str, messages, response: dict,
           tools=None, window: int | None = None) -> str:
    """Log one call and return its verdict. Never raises."""
    try:
        window = window or WINDOWS.get(model)
        chars = _chars(messages, tools)
        prompt = response.get("prompt_eval_count")
        total = (prompt or 0) + (response.get("eval_count") or 0)
        v = verdict(chars, prompt, total, window)
        row = {"at": time.time(), "caller": caller, "model": model,
               "window": window, "chars": chars, "prompt_tokens": prompt,
               "output_tokens": response.get("eval_count"),
               "chars_per_token": round(chars / prompt, 2) if prompt else None,
               "verdict": v}
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        if v == "truncated":
            print(f"[ctx_watch] {caller}: prompt TRUNCATED -- {chars:,} chars "
                  f"counted as {prompt:,} tokens against a {window} window", flush=True)
        _trim()
        return v
    except Exception:
        return "unknown"


def _trim() -> None:
    """Bound the file cheaply: rewrite only once it is well past the cap."""
    try:
        if LOG.stat().st_size < 6_000_000:
            return
        lines = LOG.read_text(encoding="utf-8").splitlines()[-KEEP_ROWS:]
        LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def summary(days: float = 7.0) -> dict:
    """Per-caller counts over the window, for the status page and the CLI."""
    cutoff = time.time() - days * 86400
    out = defaultdict(lambda: {"calls": 0, "truncated": 0, "near": 0,
                               "max_tokens": 0, "window": None, "last_truncated": None})
    try:
        lines = LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("at", 0) < cutoff:
            continue
        c = out[r.get("caller") or "?"]
        c["calls"] += 1
        c["window"] = r.get("window") or c["window"]
        total = (r.get("prompt_tokens") or 0) + (r.get("output_tokens") or 0)
        c["max_tokens"] = max(c["max_tokens"], total)
        if r.get("verdict") == "truncated":
            c["truncated"] += 1
            c["last_truncated"] = r["at"]
        elif r.get("verdict") == "near":
            c["near"] += 1
    callers = dict(out)
    return {"days": days, "callers": callers,
            "calls": sum(c["calls"] for c in callers.values()),
            "truncated": sum(c["truncated"] for c in callers.values()),
            "near": sum(c["near"] for c in callers.values())}


def main():
    s = summary()
    print(f"model context, last {s['days']:g} days: {s['calls']} calls, "
          f"{s['truncated']} truncated, {s['near']} near the limit")
    for name, c in sorted(s["callers"].items()):
        w = f" of {c['window']:,}" if c["window"] else ""
        print(f"  {name:<12} {c['calls']:>5} calls  largest {c['max_tokens']:>6,}{w}  "
              f"truncated {c['truncated']}  near {c['near']}")


if __name__ == "__main__":
    main()
