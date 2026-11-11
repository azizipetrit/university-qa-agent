"""The trace file and its Markdown view."""

import json

from fakes import answer, ask, need_query, sql

from sqlqa.cli import main
from sqlqa.config import Settings
from sqlqa.tracing import render_markdown

QUESTION = "How many teachers are there?"
COUNT = "SELECT COUNT(*) AS n FROM teachers"


def simple_run(db, tmp_path):
    return ask(db, tmp_path, QUESTION, sql(COUNT, plan="Count the rows."), answer("Four."))


def two_query_run(db, tmp_path):
    return ask(
        db,
        tmp_path,
        "How many teachers and students?",
        sql(COUNT),
        need_query("Students are still missing."),
        sql("SELECT COUNT(*) AS n FROM students"),
        answer("4 teachers and 8 students."),
    )


# --- JSON trace --------------------------------------------------------------


def test_each_run_writes_its_own_file(db, tmp_path):
    first, second = simple_run(db, tmp_path), simple_run(db, tmp_path)
    assert first.trace_path != second.trace_path
    assert len(list(tmp_path.glob("*.json"))) == 2
    assert first.trace_path.stem == first.trace["run_id"]


def test_trace_shows_the_full_path_from_question_to_answer(db, tmp_path):
    trace = simple_run(db, tmp_path).trace
    assert trace["question"] == QUESTION
    assert [step["node"] for step in trace["steps"]] == [
        "load_schema",
        "generate_sql",
        "validate_sql",
        "execute_sql",
        "answer",
    ]
    assert trace["status"] == "answered"
    assert trace["answer"] == "Four."
    assert all("duration_ms" in step for step in trace["steps"])


def test_trace_records_sql_guard_verdict_and_rows(db, tmp_path):
    load, generate, validate, execute, _ = simple_run(db, tmp_path).trace["steps"]
    assert "Table teachers" in load["update"]["schema"]
    assert generate["update"]["sql"] == COUNT
    assert validate["update"] == {"error": None}  # the guard passed it
    (query,) = execute["update"]["queries"]
    assert (query["sql"], query["columns"], query["rows"]) == (COUNT, ["n"], [[4]])


def test_trace_records_every_model_call_with_full_prompt_and_reply(db, tmp_path):
    run = simple_run(db, tmp_path)
    calls = [call for step in run.trace["steps"] for call in step["llm_calls"]]
    assert [call["schema"] for call in calls] == ["SqlDecision", "AnswerDecision"]

    generate = calls[0]
    assert generate["messages"][0][0] == "system"
    assert db.describe_schema() in generate["messages"][0][1]  # stored in full in the JSON
    assert generate["messages"][1] == ["human", f"Question: {QUESTION}"]
    assert generate["output"]["sql"] == COUNT
    assert generate["output"]["plan"] == "Count the rows."
    assert calls[1]["output"] == {"status": "answer", "answer": "Four.", "reason": None}


def test_trace_records_the_reason_for_each_query(db, tmp_path):
    trace = two_query_run(db, tmp_path).trace
    queries = [q for step in trace["steps"] for q in step["update"].get("queries", [])]
    assert [q["reason"] for q in queries] == [
        "Answer the user's question.",
        "Students are still missing.",
    ]
    assert [q["rows"] for q in queries] == [[[4]], [[8]]]


def test_trace_records_a_rejected_query_and_why(db, tmp_path):
    run = ask(db, tmp_path, QUESTION, sql("DROP TABLE teachers"), sql(COUNT), answer("Four."))
    rejected = run.trace["steps"][2]
    assert rejected["node"] == "validate_sql"
    assert rejected["update"]["error"] == "Only SELECT statements are allowed, not DROP."
    assert run.trace["steps"][1]["update"]["sql"] == "DROP TABLE teachers"


# --- Markdown view -----------------------------------------------------------


def test_markdown_shows_question_path_sql_rows_and_answer(db, tmp_path):
    markdown = render_markdown(simple_run(db, tmp_path).trace)
    assert f"**Question:** {QUESTION}" in markdown
    assert "load_schema → generate_sql → validate_sql → execute_sql → answer" in markdown
    assert f"```sql\n{COUNT}\n```" in markdown
    assert "| n |\n|---|\n| 4 |" in markdown
    assert "> Four." in markdown
    assert "### 4. execute_sql" in markdown


def test_markdown_shows_the_schema_once_not_in_every_prompt(db, tmp_path):
    run = simple_run(db, tmp_path)
    markdown = render_markdown(run.trace)
    assert markdown.count("Table teachers") == 1
    assert markdown.count("[schema: shown once above]") == 2  # one per model call
    # ...while the JSON still has it in both prompts.
    assert run.trace_path.read_text(encoding="utf-8").count("Table teachers") >= 3


def test_markdown_shows_each_query_with_its_reason(db, tmp_path):
    markdown = render_markdown(two_query_run(db, tmp_path).trace)
    assert "  - reason: Answer the user's question." in markdown
    assert "  - reason: Students are still missing." in markdown
    assert "| Model | Model calls | Queries run |" in markdown
    assert "| fake | 4 | 2 |" in markdown


def test_markdown_shows_null_and_limits_long_results(db, tmp_path):
    run = ask(
        db, tmp_path, "q", sql("SELECT id, grade FROM enrollments ORDER BY id DESC"), answer("x")
    )
    markdown = render_markdown(run.trace)
    assert "| 31 | NULL |" in markdown
    assert "(21 more rows in the JSON trace)" in markdown


def test_markdown_shows_a_failed_run(db, tmp_path):
    run = ask(
        db, tmp_path, "q", sql("SELECT * FROM a"), sql("SELECT * FROM b"), sql("SELECT * FROM c")
    )
    markdown = render_markdown(run.trace)
    assert "**Status:** failed" in markdown
    assert "give_up" in markdown
    assert "Unknown table 'c'" in markdown


# --- command line and settings -----------------------------------------------


def test_render_trace_command_writes_markdown(db, tmp_path, capsys):
    run = simple_run(db, tmp_path)
    output = tmp_path / "trace.md"
    assert main(["render-trace", str(run.trace_path), "-o", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == render_markdown(
        json.loads(run.trace_path.read_text("utf-8"))
    )


def test_settings_come_from_environment_variables(monkeypatch):
    for name in (
        "STRUCTURED_OUTPUT_METHOD",
        "SCHEMA_NOTES_FILE",
        "MAX_ROWS",
        "MAX_QUERIES",
        "TRACE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///some.db")
    monkeypatch.setenv("LLM_MODEL", "provider:model")
    monkeypatch.setenv("MAX_QUERIES", "5")

    settings = Settings.from_env()
    assert (settings.database_url, settings.llm_model) == ("sqlite:///some.db", "provider:model")
    assert settings.max_queries == 5
    assert (settings.max_rows, settings.max_attempts, settings.max_llm_calls) == (50, 3, 10)
    assert settings.structured_output_method is None


def test_missing_settings_are_named_in_the_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    try:
        Settings.from_env()
    except ValueError as exc:
        assert "DATABASE_URL, LLM_MODEL" in str(exc)
        assert ".env.example" in str(exc)
    else:
        raise AssertionError("expected a ValueError")
