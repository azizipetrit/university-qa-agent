"""The command line: ask, chat and schema. The model is scripted; nothing leaves the machine."""

import json
from pathlib import Path

import pytest
from fakes import FakeLLM, answer, clarify, sql

from sqlqa import cli
from sqlqa.config import Settings
from sqlqa.db import Database

COUNT_TEACHERS = "SELECT COUNT(*) AS n FROM teachers"
COUNT_STUDENTS = "SELECT COUNT(*) AS n FROM students"


@pytest.fixture
def environment(monkeypatch, university_db_path, tmp_path):
    """Settings a command would normally read from .env, pointing at the test database."""
    monkeypatch.setattr(cli, "load_dotenv", lambda *args, **kwargs: None)  # ignore a real .env
    for name in ("SCHEMA_NOTES_FILE", "SAMPLE_ROWS", "MAX_ROWS", "STRUCTURED_OUTPUT_METHOD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{university_db_path.as_posix()}")
    monkeypatch.setenv("LLM_MODEL", "fake")
    monkeypatch.setenv("TRACE_DIR", str(tmp_path / "traces"))
    return tmp_path / "traces"


def use_model(monkeypatch, *replies) -> FakeLLM:
    llm = FakeLLM(*replies)
    monkeypatch.setattr(cli, "create_llm", lambda model: llm)
    return llm


def type_lines(monkeypatch, *lines):
    """Feed `lines` to input(), then end the input as Ctrl+D would."""
    remaining = list(lines)

    def fake_input(prompt=""):
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


# --- ask ---------------------------------------------------------------------


def test_ask_prints_answer_sql_status_and_trace_path(environment, monkeypatch, capsys):
    use_model(monkeypatch, sql(COUNT_TEACHERS), answer("There are 4 teachers."))
    assert cli.main(["ask", "How many teachers are there?"]) == 0

    out = capsys.readouterr().out
    (trace_file,) = environment.glob("*.json")
    assert "There are 4 teachers." in out
    assert f"Query 1: {COUNT_TEACHERS}" in out
    assert "Status: answered" in out
    assert f"Trace:  {trace_file}" in out


def test_ask_exits_with_1_when_the_run_fails(environment, monkeypatch, capsys):
    use_model(monkeypatch, sql("SELECT * FROM a"), sql("SELECT * FROM b"), sql("SELECT * FROM c"))
    assert cli.main(["ask", "q"]) == 1
    assert "Status: failed" in capsys.readouterr().out


def test_missing_settings_exit_with_2_and_say_what_is_missing(environment, monkeypatch, capsys):
    monkeypatch.delenv("LLM_MODEL")
    assert cli.main(["ask", "q"]) == 2
    assert "Missing setting(s): LLM_MODEL" in capsys.readouterr().err


# --- a model call that fails ---------------------------------------------------


class Unreachable(FakeLLM):
    """A model whose every call fails, as it does with a missing or wrong key."""

    def with_structured_output(self, schema, include_raw=False, **options):
        raise ConnectionError("Connection error.")


def test_failed_model_call_prints_one_line_not_a_traceback(environment, monkeypatch, capsys):
    monkeypatch.setenv("LLM_MODEL", "anthropic:some-model")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "create_llm", lambda model: Unreachable())

    assert cli.main(["ask", "How many teachers are there?"]) == 3

    captured = capsys.readouterr()
    (trace_file,) = environment.glob("*.json")
    assert captured.out == ""
    assert captured.err.count("\n") == 1  # exactly one line
    assert "Traceback" not in captured.err
    assert captured.err.strip() == (
        "The model call failed (ConnectionError: Connection error.). "
        "Likely cause: ANTHROPIC_API_KEY is not set. Put your key in .env. "
        f"Trace: {trace_file}"
    )


def test_trace_of_a_failed_model_call_is_still_written(environment, monkeypatch, capsys):
    monkeypatch.setattr(cli, "create_llm", lambda model: Unreachable())
    cli.main(["ask", "How many teachers are there?"])

    (trace_file,) = environment.glob("*.json")
    trace = json.loads(trace_file.read_text(encoding="utf-8"))
    assert trace["status"] == "crashed"
    assert trace["question"] == "How many teachers are there?"
    assert "Connection error." in trace["error"]
    assert [step["node"] for step in trace["steps"]] == ["load_schema", "generate_sql"]


def test_likely_cause_changes_when_a_key_is_set(environment, monkeypatch, capsys):
    monkeypatch.setenv("LLM_MODEL", "anthropic:some-model")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key-that-is-set")
    monkeypatch.setattr(cli, "create_llm", lambda model: Unreachable())
    assert cli.main(["ask", "q"]) == 3
    err = capsys.readouterr().err
    assert "the key in ANTHROPIC_API_KEY is wrong or expired, or the provider cannot be" in err
    assert "a-key-that-is-set" not in err  # the key itself is never printed


def test_key_name_follows_the_configured_provider(environment, monkeypatch, capsys):
    monkeypatch.setenv("LLM_MODEL", "openai:some-model")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli, "create_llm", lambda model: Unreachable())
    cli.main(["ask", "q"])
    assert "Likely cause: OPENAI_API_KEY is not set." in capsys.readouterr().err


def test_model_that_cannot_be_created_is_reported_the_same_way(environment, monkeypatch, capsys):
    def broken(model):
        raise ImportError("No module named 'langchain_someprovider'")

    monkeypatch.setattr(cli, "create_llm", broken)
    assert cli.main(["ask", "q"]) == 3
    err = capsys.readouterr().err
    assert err.startswith("The model call failed (ImportError: No module named")
    assert "Traceback" not in err
    assert list(environment.glob("*.json")) == []  # no run started, so no trace


def test_unreadable_database_is_named_as_the_cause(environment, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'missing' / 'x.db').as_posix()}")
    use_model(monkeypatch)
    assert cli.main(["ask", "q"]) == 3
    err = capsys.readouterr().err
    assert err.startswith("The database could not be read (OperationalError:")
    assert "Likely cause: DATABASE_URL is wrong." in err


# --- chat --------------------------------------------------------------------


def test_chat_answers_each_question_and_prints_a_trace_path_each_time(
    environment, monkeypatch, capsys
):
    use_model(
        monkeypatch,
        sql(COUNT_TEACHERS),
        answer("There are 4 teachers."),
        sql(COUNT_STUDENTS),
        answer("There are 8 students."),
    )
    type_lines(monkeypatch, "How many teachers are there?", "And students?", "exit")
    assert cli.main(["chat"]) == 0

    out = capsys.readouterr().out
    traces = sorted(environment.glob("*.json"))
    assert len(traces) == 2  # one trace file per question
    assert out.index("There are 4 teachers.") < out.index("There are 8 students.")
    assert out.count("Trace:  ") == 2
    assert all(f"Trace:  {trace}" in out for trace in traces)


def test_chat_says_at_the_start_that_questions_are_independent(environment, monkeypatch, capsys):
    use_model(monkeypatch)
    type_lines(monkeypatch, "exit")
    cli.main(["chat"])
    out = capsys.readouterr().out
    assert out.startswith("Ask a question about the database.")
    assert "Each question is answered on its own" in out
    assert "earlier questions and answers are not remembered" in out


def test_chat_does_not_carry_one_question_into_the_next(environment, monkeypatch):
    llm = use_model(
        monkeypatch,
        sql(COUNT_TEACHERS),
        answer("There are 4 teachers."),
        clarify("Which students do you mean?"),
    )
    type_lines(monkeypatch, "How many teachers are there?", "And students?")
    cli.main(["chat"])

    second_question_prompt = llm.calls[2]["human"]
    assert second_question_prompt == "Question: And students?"
    assert "teachers" not in second_question_prompt  # nothing from the first question or answer


def test_chat_skips_blank_lines_and_ends_on_end_of_input(environment, monkeypatch, capsys):
    use_model(monkeypatch, sql(COUNT_TEACHERS), answer("There are 4 teachers."))
    type_lines(monkeypatch, "", "   ", "How many teachers are there?")  # then end of input
    assert cli.main(["chat"]) == 0
    assert len(list(environment.glob("*.json"))) == 1
    assert "There are 4 teachers." in capsys.readouterr().out


@pytest.mark.parametrize("word", ["exit", "quit", "EXIT"])
def test_chat_ends_on_exit_or_quit_without_calling_the_model(environment, monkeypatch, word):
    llm = use_model(monkeypatch)
    type_lines(monkeypatch, word, "this line is never read")
    assert cli.main(["chat"]) == 0
    assert llm.calls == []


def test_chat_keeps_going_after_a_question_that_crashes(environment, monkeypatch, capsys):
    class FailsOnce(FakeLLM):
        failed = False

        def with_structured_output(self, schema, include_raw=False, **options):
            if not self.failed:
                self.failed = True
                raise ConnectionError("model provider unreachable")
            return super().with_structured_output(schema)

    llm = FailsOnce(sql(COUNT_TEACHERS), answer("There are 4 teachers."))
    monkeypatch.setattr(cli, "create_llm", lambda model: llm)
    type_lines(monkeypatch, "first question", "How many teachers are there?", "exit")
    assert cli.main(["chat"]) == 0

    captured = capsys.readouterr()
    assert "The model call failed (ConnectionError: model provider unreachable)." in captured.err
    assert "There are 4 teachers." in captured.out
    assert len(list(environment.glob("*.json"))) == 2  # the crashed run left a trace too


# --- schema ------------------------------------------------------------------


def test_schema_prints_exactly_what_the_model_is_shown(
    environment, monkeypatch, capsys, university_db_path
):
    llm = use_model(monkeypatch, sql(COUNT_TEACHERS), answer("There are 4 teachers."))
    assert cli.main(["schema"]) == 0
    printed = capsys.readouterr().out

    expected = Database(f"sqlite:///{university_db_path.as_posix()}").describe_schema()
    assert printed == expected + "\n"
    assert printed.startswith("SQL dialect: sqlite")

    # ...and it is, word for word, inside the prompt of a real run.
    cli.main(["ask", "How many teachers are there?"])
    assert expected in llm.calls[0]["system"]


def test_schema_honours_the_notes_file_and_the_sample_rows_setting(
    environment, monkeypatch, capsys, tmp_path
):
    notes = tmp_path / "notes.md"
    notes.write_text("- A note about the data.", encoding="utf-8")
    monkeypatch.setenv("SCHEMA_NOTES_FILE", str(notes))
    monkeypatch.setenv("SAMPLE_ROWS", "0")
    cli.main(["schema"])
    printed = capsys.readouterr().out
    assert printed.rstrip().endswith("Notes:\n- A note about the data.")
    assert "Sample rows" not in printed


def test_schema_needs_no_model_setting_and_calls_no_model(environment, monkeypatch, capsys):
    monkeypatch.delenv("LLM_MODEL")
    monkeypatch.setattr(cli, "create_llm", lambda model: pytest.fail("no model is needed"))
    assert cli.main(["schema"]) == 0
    assert "Table teachers" in capsys.readouterr().out


def test_schema_still_needs_a_database(environment, monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL")
    assert cli.main(["schema"]) == 2
    assert "Missing setting(s): DATABASE_URL" in capsys.readouterr().err


# --- .env.example ------------------------------------------------------------


def active_settings() -> dict[str, str]:
    """The uncommented NAME=value lines of .env.example."""
    lines = (Path(__file__).parent.parent / ".env.example").read_text(encoding="utf-8").splitlines()
    pairs = [line.split("=", 1) for line in lines if "=" in line and not line.startswith("#")]
    return {name.strip(): value.split("#")[0].strip() for name, value in pairs}


def test_env_example_leaves_langsmith_switched_off():
    """Copying .env.example to .env must not send runs anywhere but the model provider."""
    assert [name for name in active_settings() if name.startswith(("LANGSMITH", "LANGCHAIN"))] == []


def test_env_example_ships_no_key_and_documents_every_setting():
    settings = active_settings()
    assert settings["ANTHROPIC_API_KEY"] == ""
    assert {
        "DATABASE_URL",
        "LLM_MODEL",
        "SCHEMA_NOTES_FILE",
        "STRUCTURED_OUTPUT_METHOD",
        "SAMPLE_ROWS",
        "MAX_ROWS",
        "MAX_ATTEMPTS",
        "MAX_QUERIES",
        "MAX_LLM_CALLS",
        "MAX_FILTER_PROBES",
        "FILTER_VALUE_LIMIT",
        "TRACE_DIR",
    } <= set(settings)


def test_env_example_values_load_as_settings(monkeypatch):
    for name, value in active_settings().items():
        monkeypatch.setenv(name, value)
    loaded = Settings.from_env()
    assert loaded.llm_model == "anthropic:claude-haiku-5-5"
    assert (loaded.max_rows, loaded.max_attempts, loaded.max_queries) == (50, 3, 3)
    assert (loaded.max_llm_calls, loaded.max_filter_probes, loaded.filter_value_limit) == (
        10,
        5,
        10,
    )
