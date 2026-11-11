"""End-to-end behaviour of the graph: real database, real guard, scripted model."""

import pytest
from fakes import MALFORMED, FakeLLM, answer, ask, clarify, final, need_query, sql

from sqlqa.config import Settings
from sqlqa.graph import build_graph, run
from sqlqa.llm import AnswerDecision, FinalAnswer
from sqlqa.tracing import Tracer, render_markdown

HAPPY_PATH = [
    "load_schema",
    "generate_sql",
    "validate_sql",
    "execute_sql",
    "answer",
    "verify_answer",
]
VERIFY = ["verify_answer"]  # every answered run ends with it
ONE_QUERY = ["generate_sql", "validate_sql", "execute_sql", "answer"]
CHECKED_QUERY = ["generate_sql", "validate_sql", "execute_sql", "check_filters", "answer"]

AVG_CS101 = """
    SELECT AVG(e.grade) AS average
    FROM enrollments e
    JOIN course_offerings o ON o.id = e.offering_id
    JOIN courses c ON c.id = o.course_id
    WHERE c.code = 'CS101'
"""


# --- the normal path ---------------------------------------------------------


def test_single_query_question(db, tmp_path):
    run = ask(db, tmp_path, "What is the average grade in CS101?", sql(AVG_CS101), answer("73.75."))
    assert run.path == HAPPY_PATH
    assert run.state["status"] == "answered"
    assert run.state["answer"] == "73.75."
    assert run.state["queries"][0]["rows"] == [[73.75]]
    assert run.state["llm_calls"] == 2  # one to write SQL, one to answer; nothing extra


def test_same_graph_answers_questions_about_an_unrelated_database(bookstore_db, tmp_path):
    run = ask(
        bookstore_db,
        tmp_path,
        "Who wrote the cheapest book?",
        sql("SELECT a.name FROM books b JOIN authors a ON a.id = b.author_id ORDER BY b.price"),
        answer("Italo Calvino."),
    )
    assert run.path == HAPPY_PATH
    assert run.state["queries"][0]["rows"][0] == ["Italo Calvino"]


# --- repair loop -------------------------------------------------------------


def test_database_error_is_repaired(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "How many students are there?",
        sql("SELECT COUNT(*) FROM students WHERE active = 1"),  # no such column
        sql("SELECT COUNT(*) FROM students"),
        answer("Eight."),
    )
    assert run.path == [
        "load_schema",
        "generate_sql",
        "validate_sql",
        "execute_sql",  # the database rejects it
        "generate_sql",
        "validate_sql",
        "execute_sql",
        "answer",
        "verify_answer",
    ]
    assert run.state["status"] == "answered"
    assert len(run.state["queries"]) == 1  # only the query that worked is recorded
    assert run.state["queries"][0]["rows"] == [[8]]


def test_write_statement_is_blocked_before_it_reaches_the_database(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "Remove all enrollments.",
        sql("DELETE FROM enrollments"),
        sql("SELECT COUNT(*) FROM enrollments"),
        answer("I can only read data. There are 31 enrollments."),
    )
    assert run.path[:4] == ["load_schema", "generate_sql", "validate_sql", "generate_sql"]
    assert "execute_sql" not in run.path[:4]
    assert "not DELETE" in run.trace["steps"][2]["update"]["error"]
    assert run.state["queries"][0]["rows"] == [[31]]  # nothing was deleted


def test_gives_up_after_three_failed_attempts(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "How many students are there?",
        sql("SELECT * FROM pupils"),
        sql("SELECT * FROM learners"),
        sql("SELECT * FROM scholars"),
    )
    assert run.path[-1] == "give_up"
    assert run.path.count("generate_sql") == 3
    assert run.state["status"] == "failed"
    assert "no valid query was produced in 3 attempts" in run.state["answer"]
    assert "Unknown table 'scholars'" in run.state["answer"]
    assert "Last SQL: SELECT * FROM scholars" in run.state["answer"]


def test_attempt_counter_starts_again_for_each_new_query(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "q",
        sql("SELECT * FROM pupils"),
        sql("SELECT * FROM learners"),
        sql("SELECT COUNT(*) FROM students"),  # third attempt works
        need_query("Also count the teachers."),
        sql("SELECT * FROM staff"),
        sql("SELECT * FROM faculty"),
        sql("SELECT COUNT(*) FROM teachers"),  # third attempt of the *second* query works
        answer("8 students, 4 teachers."),
    )
    assert run.state["status"] == "answered"
    assert [q["rows"] for q in run.state["queries"]] == [[[8]], [[4]]]


# --- empty and NULL results --------------------------------------------------


def test_empty_result_can_be_the_answer_with_no_extra_model_call(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "Which students have a grade in Fall 2026?",
        sql(
            "SELECT e.student_id FROM enrollments e "
            "JOIN course_offerings o ON o.id = e.offering_id "
            "JOIN semesters s ON s.id = o.semester_id "
            "WHERE s.name = 'Fall 2026' AND e.grade IS NOT NULL"
        ),
        answer("None yet. Fall 2026 is still in progress."),
    )
    assert run.path == ["load_schema", *CHECKED_QUERY, *VERIFY]
    assert run.state["queries"][0]["rows"] == []
    assert run.state["llm_calls"] == 2  # check_filters does not call the model


def test_empty_result_from_a_wrong_filter_is_corrected_with_a_second_query(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "How many offerings were there in fall 2025?",
        sql(
            "SELECT o.id FROM course_offerings o JOIN semesters s ON s.id = o.semester_id "
            "WHERE s.name = 'fall 2025'"
        ),
        need_query("0 rows: the sample rows write it as 'Fall 2025', with a capital F."),
        sql(
            "SELECT COUNT(*) FROM course_offerings o JOIN semesters s ON s.id = o.semester_id "
            "WHERE s.name = 'Fall 2025'"
        ),
        answer("Three."),
    )
    assert run.path == ["load_schema", *CHECKED_QUERY, *ONE_QUERY, *VERIFY]
    assert [q["rows"] for q in run.state["queries"]] == [[], [[3]]]
    assert run.state["queries"][1]["reason"].startswith("0 rows:")


def test_all_null_result_reaches_the_answer_node_as_null_not_zero(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "What is the average grade in Fall 2026?",
        sql(
            "SELECT AVG(e.grade) FROM enrollments e "
            "JOIN course_offerings o ON o.id = e.offering_id WHERE o.semester_id = 3"
        ),
        answer("There is no average yet: no Fall 2026 enrollment has been graded."),
    )
    assert run.state["queries"][0]["rows"] == [[None]]


# --- check_filters: evidence for empty results ---------------------------------


def test_check_filters_runs_only_when_the_result_is_empty(db, tmp_path):
    full = ask(db, tmp_path, "q", sql("SELECT name FROM teachers"), answer("Four."))
    assert "check_filters" not in full.path
    assert full.state["queries"][0]["filter_check"] is None

    empty = ask(
        db, tmp_path, "q", sql("SELECT name FROM teachers WHERE name = 'Nobody'"), answer("None.")
    )
    assert empty.path == ["load_schema", *CHECKED_QUERY, *VERIFY]


def test_missing_value_is_reported_with_the_values_that_exist(db, tmp_path):
    """Case 1: the query asks for a value the column does not hold."""
    run = ask(
        db,
        tmp_path,
        "How many offerings were there in the winter term?",
        sql(
            "SELECT o.id FROM course_offerings o JOIN semesters s ON s.id = o.semester_id "
            "WHERE s.term = 'Winter'"
        ),
        answer("There is no winter term. The terms are Fall and Spring."),
    )
    finding = run.state["queries"][0]["filter_check"]
    assert finding == {
        "checked": [
            {
                "filter": "semesters.term = 'Winter'",
                "found": False,
                "detail": "no row has this value; the column holds only: 'Fall', 'Spring'",
            }
        ],
        "not_checked": 0,
    }
    # The answer step is shown the finding, word for word.
    prompt = run.llm.calls[1]["human"]
    assert "Result: 0 rows. The query matched nothing." in prompt
    assert "- semesters.term = 'Winter': no row has this value; the column holds only:" in prompt


def test_wrongly_cased_value_is_reported_with_the_real_spelling(db, tmp_path):
    """Case 2: the value exists, written differently. The second query uses the real one."""
    run = ask(
        db,
        tmp_path,
        "What is the average grade in math201?",
        sql(
            "SELECT AVG(e.grade) FROM enrollments e "
            "JOIN course_offerings o ON o.id = e.offering_id "
            "JOIN courses c ON c.id = o.course_id WHERE c.code = 'math201'"
        ),
        need_query("The check shows the code is written 'MATH201'."),
        sql(
            "SELECT AVG(e.grade) FROM enrollments e "
            "JOIN course_offerings o ON o.id = e.offering_id "
            "JOIN courses c ON c.id = o.course_id WHERE c.code = 'MATH201'"
        ),
        answer("86.0."),
    )
    first, second = run.state["queries"]
    assert first["rows"] == [[None]]  # an aggregate over no rows: one row of NULL, not 0 rows
    assert first["filter_check"]["checked"] == [
        {
            "filter": "courses.code = 'math201'",
            "found": False,
            "detail": "no row has this value, but 'MATH201' exists (different case)",
        }
    ]
    assert "but 'MATH201' exists (different case)" in run.llm.calls[1]["human"]
    assert second["rows"] == [[86.0]]
    assert second["filter_check"] is None
    assert run.path == ["load_schema", *CHECKED_QUERY, *ONE_QUERY, *VERIFY]


def test_real_empty_result_is_confirmed_when_every_value_exists(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "Which grades are recorded for Fall 2026?",
        sql(
            "SELECT e.grade FROM enrollments e "
            "JOIN course_offerings o ON o.id = e.offering_id "
            "JOIN semesters s ON s.id = o.semester_id "
            "WHERE s.name = 'Fall 2026' AND e.grade IS NOT NULL"
        ),
        answer("None yet."),
    )
    assert run.state["queries"][0]["filter_check"] == {
        "checked": [{"filter": "semesters.name = 'Fall 2026'", "found": True, "detail": "found"}],
        "not_checked": 0,
    }


def test_finding_is_in_the_trace_and_the_markdown_view(db, tmp_path):
    run = ask(
        db, tmp_path, "q", sql("SELECT id FROM semesters WHERE term = 'Winter'"), answer("None.")
    )
    execute, check = run.trace["steps"][3], run.trace["steps"][4]
    assert execute["node"] == "execute_sql"
    assert execute["update"]["pending"]["rows"] == []  # held back, not recorded yet
    assert "queries" not in execute["update"]
    assert check["node"] == "check_filters"
    assert check["llm_calls"] == []
    assert check["update"]["queries"][0]["filter_check"]["checked"][0]["found"] is False
    assert check["update"]["pending"] is None

    markdown = render_markdown(run.trace)
    assert "### 5. check_filters" in markdown
    assert "- semesters.term = 'Winter': no row has this value; the column holds only:" in markdown


def test_value_listing_can_be_switched_off(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "q",
        sql("SELECT id FROM semesters WHERE term = 'Winter'"),
        answer("None."),
        filter_value_limit=0,
    )
    (item,) = run.state["queries"][0]["filter_check"]["checked"]
    assert item == {
        "filter": "semesters.term = 'Winter'",
        "found": False,
        "detail": "no row has this value",
    }
    assert "Fall" not in run.llm.calls[1]["human"].split("Filter check")[1]


def test_check_filters_respects_the_model_call_limit(db, tmp_path):
    run = ask(
        db, tmp_path, "q", sql("SELECT id FROM semesters WHERE term = 'Winter'"), max_llm_calls=1
    )
    assert run.path[-2:] == ["check_filters", "give_up"]
    assert run.state["queries"][0]["filter_check"] is not None


def test_same_check_works_on_an_unrelated_database(bookstore_db, tmp_path):
    run = ask(
        bookstore_db,
        tmp_path,
        "q",
        sql("SELECT title FROM books b JOIN authors a ON a.id = b.author_id WHERE a.name = 'x'"),
        answer("No such author."),
    )
    (item,) = run.state["queries"][0]["filter_check"]["checked"]
    assert item["filter"] == "authors.name = 'x'"
    assert item["detail"].endswith("the column holds only: 'Ursula Le Guin', 'Italo Calvino'")


# --- verify_answer: figures in the answer must come from the run ---------------


def test_answer_with_supported_figures_is_left_alone(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "What is the average grade in CS101?",
        sql(AVG_CS101),
        answer("The average grade in CS101 is 73.75."),
    )
    assert run.path[-1] == "verify_answer"
    assert run.state["verification"] == {"checked": 1, "unsupported": []}
    assert run.state["answer"] == "The average grade in CS101 is 73.75."  # no note
    assert run.state["llm_calls"] == 2  # verify_answer does not call the model


def test_figure_that_is_not_in_the_results_gets_a_note(db, tmp_path):
    """The real case that prompted this check: a date taken from the sample rows."""
    run = ask(
        db,
        tmp_path,
        "What is the average grade in the latest semester?",
        sql(
            "SELECT s.name, AVG(e.grade) AS average FROM enrollments e "
            "JOIN course_offerings o ON o.id = e.offering_id "
            "JOIN semesters s ON s.id = o.semester_id WHERE s.id = 3 GROUP BY s.name"
        ),
        answer("The latest semester is Fall 2026 (start date 2026-09-07). No average yet."),
    )
    assert run.state["queries"][0]["rows"] == [["Fall 2026", None]]
    assert run.state["verification"] == {"checked": 2, "unsupported": ["2026-09-07"]}
    assert run.state["answer"] == (
        "The latest semester is Fall 2026 (start date 2026-09-07). No average yet."
        "\n\nNote: these figures were not found in the query results and could not be "
        "verified: 2026-09-07."
    )
    assert run.state["status"] == "answered"  # the status does not change
    assert run.state["llm_calls"] == 2  # and the model is not asked again


def test_wrong_number_gets_a_note(db, tmp_path):
    run = ask(db, tmp_path, "q", sql(AVG_CS101), answer("The average is 78.4, from 12 grades."))
    assert run.state["verification"]["unsupported"] == ["78.4", "12"]
    assert run.state["answer"].endswith("could not be verified: 78.4, 12.")


def test_verification_is_in_the_trace_and_the_markdown_view(db, tmp_path):
    run = ask(db, tmp_path, "q", sql(AVG_CS101), answer("The average is 78.4."))
    step = run.trace["steps"][-1]
    assert step["node"] == "verify_answer"
    assert step["llm_calls"] == []
    assert step["update"]["verification"] == {"checked": 1, "unsupported": ["78.4"]}
    assert "could not be verified: 78.4." in step["update"]["answer"]
    assert run.trace["answer"] == run.state["answer"]  # the trace header has the note too

    markdown = render_markdown(run.trace)
    assert "### 6. verify_answer" in markdown
    assert '- `verification`: {"checked": 1, "unsupported": ["78.4"]}' in markdown


def test_figures_from_the_filter_check_and_the_question_are_supported(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "How many offerings were there in 2024?",
        sql(
            "SELECT o.id FROM course_offerings o JOIN semesters s ON s.id = o.semester_id "
            "WHERE s.year = 2024"
        ),
        answer("None. There is no 2024 semester; the years in the data are 2025 and 2026."),
    )
    detail = run.state["queries"][0]["filter_check"]["checked"][0]["detail"]
    assert detail == "no row has this value; the column holds only: 2025, 2026"
    assert run.state["verification"] == {"checked": 3, "unsupported": []}


def test_runs_that_do_not_end_in_an_answer_are_not_verified(db, tmp_path):
    asked = ask(db, tmp_path, "How did they do?", clarify("Who do you mean? Give 1 name."))
    assert "verify_answer" not in asked.path
    assert "verification" not in asked.state

    failed = ask(db, tmp_path, "q", sql("SELECT * FROM a"), max_llm_calls=1)
    assert failed.path[-1] == "give_up"
    assert "verify_answer" not in failed.path


def test_note_added_at_the_query_limit_is_not_itself_checked(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "q",
        sql("SELECT 41"),
        final("The value is 41.", complete=False),
        max_queries=1,
    )
    # The limit note says "1 queries"; that sentence is the code's, not the model's.
    assert run.state["verification"] == {"checked": 1, "unsupported": []}
    assert run.state["answer"].endswith("The limit of 1 queries per question was reached.")


# --- multi-step loop ---------------------------------------------------------


def test_second_query_is_run_when_the_answer_node_asks_for_it(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "What is Daniel Kim's average grade?",
        sql("SELECT id, email FROM students WHERE name = 'Daniel Kim'"),
        need_query("Two students share this name. Get the average for each id."),
        sql(
            "SELECT s.email, AVG(e.grade) FROM students s "
            "JOIN enrollments e ON e.student_id = s.id WHERE s.id IN (4, 6) GROUP BY s.id"
        ),
        answer("There are two: daniel.kim@ averages 62.5, daniel.kim2@ averages 91.3."),
    )
    assert run.path == ["load_schema", *ONE_QUERY, *ONE_QUERY, *VERIFY]
    assert run.state["status"] == "answered"
    assert len(run.state["queries"]) == 2
    assert run.state["queries"][0]["reason"] == "Answer the user's question."
    assert run.state["queries"][1]["reason"].startswith("Two students share this name.")
    assert run.state["llm_calls"] == 4


def test_at_the_query_limit_the_model_can_only_answer(db, tmp_path):
    count = "SELECT COUNT(*) FROM {}"
    run = ask(
        db,
        tmp_path,
        "How big is everything?",
        sql(count.format("students")),
        need_query("Now teachers."),
        sql(count.format("teachers")),
        need_query("Now courses."),
        sql(count.format("courses")),
        final("8 students, 4 teachers, 6 courses. Semesters were not counted.", complete=False),
    )
    assert len(run.state["queries"]) == 3
    # The first two answer calls may ask for more; the third is given a schema that cannot.
    assert [c["schema"] for c in run.llm.calls[1::2]] == [
        AnswerDecision,
        AnswerDecision,
        FinalAnswer,
    ]
    assert "The limit of 3 queries is reached" in run.llm.calls[-1]["human"]
    assert run.state["status"] == "answered"
    assert run.state["answer"].startswith("8 students, 4 teachers, 6 courses.")
    assert run.state["answer"].endswith("The limit of 3 queries per question was reached.")


def test_no_note_is_added_when_the_answer_at_the_limit_is_complete(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "q",
        sql("SELECT 1"),
        need_query("more"),
        sql("SELECT 2"),
        final("Done."),
        max_queries=2,
    )
    assert run.state["answer"] == "Done."


# --- overall model-call limit ------------------------------------------------


def test_run_stops_at_the_model_call_limit(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "q",
        sql("SELECT COUNT(*) FROM students"),
        need_query("more"),
        sql("SELECT COUNT(*) FROM teachers"),
        need_query("more"),
        max_llm_calls=4,
    )
    assert run.path[-1] == "give_up"
    assert run.state["llm_calls"] == 4
    assert run.state["status"] == "failed"
    assert "the limit of 4 model calls per question was reached" in run.state["answer"]
    assert "2 earlier query(ies) did run" in run.state["answer"]


def test_model_call_limit_also_stops_the_repair_loop(db, tmp_path):
    run = ask(db, tmp_path, "q", sql("SELECT * FROM a"), sql("SELECT * FROM b"), max_llm_calls=2)
    assert run.path[-1] == "give_up"
    assert "limit of 2 model calls" in run.state["answer"]


# --- other endings -----------------------------------------------------------


def test_ambiguous_question_ends_with_a_clarifying_question(db, tmp_path):
    run = ask(
        db, tmp_path, "Who is the best?", clarify("Best by average grade, or by courses taken?")
    )
    assert run.path == ["load_schema", "generate_sql"]
    assert run.state["status"] == "clarify"
    assert run.state["answer"] == "Best by average grade, or by courses taken?"


def test_malformed_answer_ends_the_run_as_failed(db, tmp_path):
    run = ask(db, tmp_path, "q", sql("SELECT 1"), MALFORMED)
    assert run.state["status"] == "failed"
    assert "did not match the required format" in run.state["answer"]


def test_a_crash_inside_a_node_is_raised_and_still_traced(db, tmp_path):
    class BrokenLLM(FakeLLM):
        def with_structured_output(self, *args, **kwargs):
            raise ConnectionError("model provider unreachable")

    settings = Settings(database_url="unused", llm_model="fake", trace_dir=str(tmp_path))
    with pytest.raises(ConnectionError):
        run("q", BrokenLLM(), db, settings)

    (trace_file,) = tmp_path.glob("*.json")
    text = trace_file.read_text(encoding="utf-8")
    assert '"status": "crashed"' in text
    assert "model provider unreachable" in text


# --- graph shape -------------------------------------------------------------


def test_graph_has_exactly_the_documented_nodes(db, tmp_path):
    settings = Settings(database_url="unused", llm_model="fake", trace_dir=str(tmp_path))
    graph = build_graph(FakeLLM(), db, Tracer(tmp_path, "q", "fake"), settings)
    nodes = set(graph.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {
        "load_schema",
        "generate_sql",
        "validate_sql",
        "execute_sql",
        "check_filters",
        "answer",
        "verify_answer",
        "give_up",
    }
