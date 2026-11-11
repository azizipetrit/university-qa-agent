"""Tracing: one JSON file per run, and a Markdown view of it.

The JSON keeps everything (full prompts, raw model output, every returned row).
The Markdown is for reading: the schema is shown once, not inside each prompt.
"""

import json
import time
import uuid
from datetime import datetime
from pathlib import Path

ROWS_SHOWN = 10  # rows per result table in the Markdown view
SCHEMA_PLACEHOLDER = "[schema: shown once above]"


class Tracer:
    """Records one run. Create a new one for each question."""

    def __init__(self, trace_dir: str | Path, question: str, model: str):
        now = datetime.now()
        self.run_id = f"{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.path = Path(trace_dir) / f"{self.run_id}.json"
        self._started = time.perf_counter()
        self._step = None  # the node currently running
        self.trace = {
            "run_id": self.run_id,
            "started_at": now.isoformat(timespec="seconds"),
            "question": question,
            "model": model,
            "steps": [],
        }

    def wrap(self, name: str, node):
        """Return `node` with recording around it. Applied to every node in graph.py."""

        def traced(state):
            step = {"node": name, "llm_calls": []}
            self._step = step
            self.trace["steps"].append(step)
            started = time.perf_counter()
            try:
                update = node(state)
                step["update"] = update  # what the node changed in the state
                return update
            except Exception as exc:
                step["error"] = repr(exc)
                raise
            finally:
                step["duration_ms"] = round((time.perf_counter() - started) * 1000)

        return traced

    def llm_call(self, **record):
        """Called by call_llm; attaches the call to the node that made it."""
        self._step["llm_calls"].append(record)

    def finish(self, state: dict | None = None, error: str | None = None) -> Path:
        """Write the trace file and return its path."""
        state = state or {}
        self.trace["status"] = "crashed" if error else state.get("status")
        self.trace["answer"] = state.get("answer")
        self.trace["error"] = error
        self.trace["duration_ms"] = round((time.perf_counter() - self._started) * 1000)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.trace, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        return self.path


def render_markdown(trace: dict) -> str:
    """Turn a trace (the loaded JSON) into a readable Markdown report."""
    steps = trace["steps"]
    calls = [call for step in steps for call in step["llm_calls"]]
    queries = [q for step in steps for q in (step.get("update") or {}).get("queries", [])]
    schema = next(
        (step["update"]["schema"] for step in steps if "schema" in (step.get("update") or {})), ""
    )
    tokens_in = sum((call["usage"] or {}).get("input_tokens", 0) for call in calls)
    tokens_out = sum((call["usage"] or {}).get("output_tokens", 0) for call in calls)

    out = [
        f"# Trace {trace['run_id']}",
        "",
        f"**Question:** {trace['question']}",
        "",
        f"**Status:** {trace['status']}",
        "",
        "**Answer:**",
        "",
        _quote(trace.get("answer") or trace.get("error") or "(none)"),
        "",
        "| Model | Model calls | Queries run | Tokens in / out | Duration |",
        "|---|---|---|---|---|",
        f"| {trace['model']} | {len(calls)} | {len(queries)} | {tokens_in} / {tokens_out} "
        f"| {_seconds(trace['duration_ms'])} |",
        "",
        "## Path",
        "",
        " → ".join(step["node"] for step in steps),
        "",
        "## Schema given to the model",
        "",
        "Shown once here. Each prompt below contained it in full.",
        "",
        _code(schema),
        "",
        "## Steps",
    ]

    for number, step in enumerate(steps, start=1):
        out += [
            "",
            f"### {number}. {step['node']} ({_seconds(step.get('duration_ms', 0))})",
            "",
        ]
        for call in step["llm_calls"]:
            out += _render_llm_call(call, schema)
        if "error" in step:
            out += [f"**Node raised:** `{step['error']}`", ""]
        out += _render_update(step.get("update") or {})
    return "\n".join(out).rstrip() + "\n"


def _render_llm_call(call: dict, schema: str) -> list[str]:
    usage = call["usage"] or {}
    out = [
        f"**Model call** ({call['schema']}, {usage.get('input_tokens', '?')} tokens in, "
        f"{usage.get('output_tokens', '?')} out, {_seconds(call['duration_ms'])})",
        "",
    ]
    for role, content in call["messages"]:
        if schema:
            content = content.replace(schema, SCHEMA_PLACEHOLDER)
        out += [f"*Prompt, {role}:*", "", _code(content), ""]
    if call["error"]:
        out += [
            f"*Reply did not fit the schema:* `{call['error']}`",
            "",
            _code(str(call["raw"])),
            "",
        ]
    else:
        out += ["*Reply:*", "", _code(json.dumps(call["output"], indent=2), "json"), ""]
    return out


def _render_update(update: dict) -> list[str]:
    """The state changes a node made, with SQL and results formatted for reading."""
    if not update:
        return []
    out = ["**State changes:**", ""]
    for key, value in update.items():
        if key == "schema":
            out.append(f"- `schema`: {len(value)} characters (shown above)")
        elif key == "sql" and value:
            out += ["- `sql`:", "", _code(value, "sql"), ""]
        elif key == "queries":
            for query in value:
                out += _render_query(query)
        elif key == "answer":
            out += ["- `answer`:", "", _quote(value), ""]
        else:
            out.append(f"- `{key}`: {json.dumps(value, ensure_ascii=False)}")
    return out + [""]


def _render_query(query: dict) -> list[str]:
    rows, columns = query["rows"], query["columns"]
    out = [
        "- `queries`: one query recorded",
        f"  - reason: {query['reason']}",
        f"  - plan: {query['plan']}",
        f"  - result: {len(rows)} row(s)"
        + (", truncated at the row limit" if query["truncated"] else ""),
        "",
        _code(query["sql"], "sql"),
        "",
    ]
    if rows:
        out += ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
        out += [
            "| " + " | ".join(_cell(value) for value in row) + " |" for row in rows[:ROWS_SHOWN]
        ]
        if len(rows) > ROWS_SHOWN:
            out += ["", f"({len(rows) - ROWS_SHOWN} more rows in the JSON trace)"]
        out.append("")
    return out


def _cell(value) -> str:
    return "NULL" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def _code(text: str, language: str = "text") -> str:
    return f"```{language}\n{text}\n```"


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines())


def _seconds(milliseconds: int) -> str:
    return f"{milliseconds / 1000:.1f} s" if milliseconds >= 1000 else f"{milliseconds} ms"
