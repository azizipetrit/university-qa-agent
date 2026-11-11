"""Regenerate docs/traces/ and docs/EXAMPLES.md.

    uv run python scripts/make_examples.py               # rebuild Markdown from the traces
    uv run python scripts/make_examples.py --run all     # re-run every example first
    uv run python scripts/make_examples.py --run 01 03   # re-run only these

--run calls the model configured in .env.

Everything that depends on a run (model name, answer, SQL, path, counts) is read from the
trace files, so the document cannot disagree with them. The commentary under each example
is written by hand in this file: after re-running, read the new traces and check it still holds.
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import sqlglot
from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tests"))  # for the scripted model

from fakes import FakeLLM, answer, sql  # noqa: E402

from sqlqa.config import Settings  # noqa: E402
from sqlqa.db import Database  # noqa: E402
from sqlqa.graph import run  # noqa: E402
from sqlqa.llm import create_llm  # noqa: E402
from sqlqa.tracing import render_markdown  # noqa: E402

TRACES = ROOT / "docs" / "traces"
SCRIPTED = "scripted (no real model; see tests/test_agent.py)"

# name, title, question, settings that differ from .env, commentary
EXAMPLES = [
    (
        "01-join-and-aggregation",
        "Joins, aggregation and a CTE in one query",
        "Which teacher has the highest average grade across everything they have taught, "
        "and how many graded enrollments is that based on?",
        {},
        "The same value (79.0 over 6 grades) is checked by hand in `tests/test_db.py`.",
    ),
    (
        "02-duplicate-names",
        "Two people with the same name, reported separately",
        "What is Daniel Kim's average grade?",
        {},
        "The query groups by student id, not by name, so the two students are not merged. "
        "Averaging over the name would return 79.8, which describes neither of them. The prompt "
        "asks for a unique column whenever a question names a person, and the schema notes say "
        "that names repeat.",
    ),
    (
        "03-no-sample-rows",
        "An empty result leads to a second query (sample rows off)",
        "What is the average grade in the programming intro class taught by Dr. Chen in the "
        "winter term of 2025?",
        {"sample_rows": 0},
        "",  # filled in below, depends on what the run did
    ),
    (
        "04-null-result-latest-semester",
        'A NULL result is reported as "not available", not zero',
        "What is the average grade in the latest semester?",
        {},
        "The latest semester has enrollments but no grades yet, so the average is NULL. The "
        "answer prompt flags NULL results explicitly and tells the model not to report zero.",
    ),
    (
        "05-clarification",
        "An ambiguous question gets a clarifying question",
        "Who is the best?",
        {},
        "The run ends after `generate_sql`. No SQL is written or run.",
    ),
    (
        "06-refused-write",
        "A request to change data is refused",
        "Delete all students.",
        {},
        "The model declined on its own here. If it had written a `DELETE`, the guard would have "
        "rejected it (`tests/test_agent.py::"
        "test_write_statement_is_blocked_before_it_reaches_the_database`), and if the guard had "
        "missed it, the read-only connection would have refused it (`tests/test_database.py::"
        "test_writes_are_refused_by_the_connection_itself`).",
    ),
]

REPAIR = "07-repair-scripted"
DEBUG_BEFORE = "08-debug-before-fix"
DEBUG_AFTER = "09-debug-after-fix"


# --- running -----------------------------------------------------------------


def run_examples(selected: list[str]):
    load_dotenv(find_dotenv(usecwd=True))
    base = Settings.from_env()
    llm = create_llm(base.llm_model)

    for name, _title, question, overrides, _note in EXAMPLES:
        if not wanted(name, selected):
            continue
        db = Database(
            base.database_url,
            sample_rows=overrides.get("sample_rows", base.sample_rows),
            notes_file=base.schema_notes_file,
        )
        state, path = run(question, llm, db, base)
        shutil.copyfile(path, TRACES / f"{name}.json")
        print(f"{name}: {state['status']}, {len(state['queries'])} query(ies)")

    if wanted(REPAIR, selected):
        # The real model rarely writes SQL that fails on this schema, so this one is scripted.
        fake = FakeLLM(
            sql(
                "SELECT COUNT(*) AS active_students FROM students WHERE active = 1",
                plan="Count the students that are marked active.",
            ),
            sql(
                "SELECT COUNT(*) AS students FROM students",
                plan="There is no 'active' column, so count all students.",
            ),
            answer("There are 8 students. The database has no 'active' flag, so all are counted."),
        )
        settings = Settings(
            database_url=base.database_url, llm_model=SCRIPTED, trace_dir=base.trace_dir
        )
        db = Database(base.database_url, notes_file=base.schema_notes_file)
        _, path = run("How many active students are there?", fake, db, settings)
        shutil.copyfile(path, TRACES / f"{REPAIR}.json")
        print(f"{REPAIR}: scripted")


def wanted(name: str, selected: list[str]) -> bool:
    return "all" in selected or any(name.startswith(prefix) for prefix in selected)


# --- reading traces ----------------------------------------------------------


def load(name: str) -> dict:
    return json.loads((TRACES / f"{name}.json").read_text(encoding="utf-8"))


def queries(trace: dict) -> list[dict]:
    return [q for step in trace["steps"] for q in (step.get("update") or {}).get("queries", [])]


def model_calls(trace: dict) -> list[dict]:
    return [call for step in trace["steps"] for call in step["llm_calls"]]


def path_of(trace: dict) -> str:
    return " → ".join(step["node"] for step in trace["steps"])


def errors(trace: dict) -> list[tuple[str, str]]:
    """(node, error) for every step where the guard or the database rejected the SQL."""
    return [
        (step["node"], step["update"]["error"])
        for step in trace["steps"]
        if step["node"] in ("validate_sql", "execute_sql")
        and (step.get("update") or {}).get("error")
    ]


def pretty_sql(text: str) -> str:
    try:
        return sqlglot.transpile(text, read="sqlite", pretty=True)[0]
    except sqlglot.errors.SqlglotError:
        return text


def quote(text: str) -> str:
    return "\n".join(f"> {line}".rstrip() for line in text.splitlines())


# --- writing EXAMPLES.md -----------------------------------------------------


def example_section(number: int, name: str, title: str, note: str) -> list[str]:
    trace = load(name)
    out = [f"## {number}. {title}", "", f"**Question:** {trace['question']}", ""]
    out += [f"**Answer** (status `{trace['status']}`):", "", quote(trace["answer"]), ""]
    for node, error in errors(trace):
        out += [f"Rejected in `{node}`: {error}", ""]
    for index, query in enumerate(queries(trace), start=1):
        if len(queries(trace)) > 1:
            out += [f"**Query {index}.** Reason: {query['reason']}", ""]
        out += [
            "```sql",
            pretty_sql(query["sql"]),
            "```",
            "",
            f"Returned {len(query['rows'])} row(s).",
            "",
        ]
    out += [f"**Path:** `{path_of(trace)}`", ""]
    if note:
        out += [note, ""]
    out += [f"Trace: [{name}.md](traces/{name}.md), model `{trace['model']}`.", ""]
    return out


def write_examples():
    names = [e[0] for e in EXAMPLES] + [REPAIR, DEBUG_BEFORE, DEBUG_AFTER]
    for name in names:
        (TRACES / f"{name}.md").write_text(render_markdown(load(name)), encoding="utf-8")

    titles = {e[0]: e[1] for e in EXAMPLES} | {
        REPAIR: "Rejected SQL is repaired (scripted model)",
        DEBUG_BEFORE: "A wrong answer, before it was fixed",
        DEBUG_AFTER: "The same question after the fix",
    }
    out = [
        "# Examples",
        "",
        "<!-- Generated by scripts/make_examples.py from the trace files. Do not edit by hand. -->",
        "",
        f"{len(names)} runs, each committed as a JSON trace and as rendered Markdown in "
        "[`traces/`](traces/). The model named for each run is read from its trace.",
        "",
        "| # | What it shows | Model | Model calls | Queries | Trace |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for number, name in enumerate(names, start=1):
        trace = load(name)
        out.append(
            f"| {number} | {titles[name]} | `{trace['model']}` | {len(model_calls(trace))} "
            f"| {len(queries(trace))} | [{name[:2]}](traces/{name}.md) |"
        )
    out += [
        "",
        "SQL below is laid out for reading. Each trace has it exactly as the model wrote it.",
        "",
    ]

    for number, (name, title, _question, _overrides, note) in enumerate(EXAMPLES, start=1):
        out += example_section(number, name, title, NOTES.get(name, note))

    out += example_section(
        len(EXAMPLES) + 1,
        REPAIR,
        titles[REPAIR],
        "The real model rarely writes SQL that fails on this small schema, so this trace was "
        "produced with the scripted model from `tests/fakes.py`. The guard passed the first "
        "query because it checks tables, not columns. The database rejected it, the error went "
        "back to `generate_sql`, and the second query ran.",
    )
    out += debugging_section()
    (ROOT / "docs" / "EXAMPLES.md").write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    print("wrote docs/EXAMPLES.md and", len(names), "trace views")


def debugging_section() -> list[str]:
    before, after = load(DEBUG_BEFORE), load(DEBUG_AFTER)
    plan = model_calls(before)[0]["output"]["plan"]
    before_row = queries(before)[0]["rows"][0]
    after_row = queries(after)[0]["rows"][0]
    current = load(EXAMPLES[3][0])
    return [
        "## Debugging a wrong answer",
        "",
        "This is a real case from building the project, not a staged one. Both traces in this "
        f"section are from `{before['model']}`, the model in use when it happened.",
        "",
        "**The symptom.** The first full run of the live evaluation (`uv run pytest -m live`) "
        "failed on one question out of 14:",
        "",
        f"**Question:** {before['question']}",
        "",
        quote(before["answer"]),
        "",
        "The latest semester is Fall 2026, which has no grades yet. The expected result was a "
        "NULL average. The agent answered for a different semester.",
        "",
        f"**Reading the trace** ([{DEBUG_BEFORE}](traces/{DEBUG_BEFORE}.md)), step by step:",
        "",
        "| Step | What the trace shows | Verdict |",
        "| --- | --- | --- |",
        "| `load_schema` | The schema and notes are complete. The notes say to order semesters "
        "by `start_date`. | Fine |",
        f'| `generate_sql` | The plan says: "{plan.strip()}" | The fault is here |',
        "| `validate_sql` | `error: null`. It is a valid SELECT. | Fine |",
        f"| `execute_sql` | One row: `{before_row}`. The database did what it was asked. | Fine |",
        "| `answer` | Reports that row and states the assumption. | Faithful to the result |",
        "",
        "So the SQL was valid, the database was right, and the answer matched the rows. The "
        "problem was an interpretation: the model decided that a semester without grades did "
        'not count as "latest". The plan recorded in the trace says so in the model\'s own '
        "words, which is why `generate_sql` returns a plan and not only SQL.",
        "",
        "**The fix.** This is a fact about the data, so it belongs in the schema notes, not in "
        "the agent code. One line was added to `university/notes.md`:",
        "",
        "```text",
        "- The latest semester is the one with the latest start_date, whether or not it has "
        "grades yet.",
        "```",
        "",
        f"**Checking the fix.** With that line, the same model returned `{after_row}` and "
        f"answered ([{DEBUG_AFTER}](traces/{DEBUG_AFTER}.md)):",
        "",
        quote(after["answer"]),
        "",
        "The question is one of the golden questions, so a regression would be caught.",
        "",
        DEBUG_NOTE.format(current_model=current["model"], current_name=EXAMPLES[3][0]),
        "",
        "### Where to look, by symptom",
        "",
        "| Symptom | Look at | Likely cause |",
        "| --- | --- | --- |",
        "| Wrong number, valid SQL | The `plan` in the `generate_sql` reply | The question was "
        "read differently than intended |",
        "| Wrong table or join | The schema block at the top of the trace | Missing foreign key, "
        "or a missing note |",
        "| 0 rows unexpectedly | The filter values in the SQL against the sample rows | A value "
        "written differently from the data |",
        "| Run ended in `give_up` | The `error` in each `validate_sql` and `execute_sql` step | "
        "The model could not repair the SQL in 3 attempts |",
        "| Answer disagrees with the rows | The result table in `execute_sql` against the "
        "`answer` reply | The answer step misread the result |",
        "| Slow or expensive run | Token counts and durations per model call | A large schema "
        "or a long loop |",
    ]


# Commentary that depends on what the committed runs actually did. Check after re-running.
NOTES: dict[str, str] = {
    "03-no-sample-rows": (
        "Sample rows were switched off for this run (`SAMPLE_ROWS=0`), so the model could not "
        "see how terms are written. The first query returned 0 rows. The `answer` node judged "
        "that a filter mismatch was more likely than a true absence and asked for a second "
        "query, with the reason shown above. This is the multi-step loop and the empty-result "
        "check working together on a real run."
    ),
    "04-null-result-latest-semester": (
        "The latest semester has enrollments but no grades yet, so the average is NULL. The "
        "answer prompt flags NULL results and tells the model not to report zero. The query "
        "selects the semester's name next to the average, which the SQL prompt asks for, so the "
        "answer can say which semester it describes. The start date in the answer is not in "
        "the query result: the model took it from the sample rows in the schema. It is correct, "
        "but it is a value the answer step was told not to use."
    ),
}
DEBUG_NOTE = (
    "**Does it reproduce on the current default model?** No. With the fix line removed from "
    "the notes, `{current_model}` selected the true latest semester and returned NULL in three "
    "of three runs. That is why this walkthrough keeps the original traces. The line stays in "
    "the notes: it states a fact the data cannot express, and it costs one sentence. The "
    "current model's run of the same question is example 4 "
    "([{current_name}](traces/{current_name}.md))."
)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", nargs="+", default=[], metavar="NAME", help="'all' or prefixes")
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.run:
        run_examples(args.run)
    write_examples()
