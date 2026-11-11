"""check_answer on its own: which figures it reads from an answer, and what supports them."""

import pytest

from sqlqa.answer_check import check_answer, find_figures


def query(rows, filter_check=None):
    return {"rows": rows, "filter_check": filter_check}


def unsupported(answer, rows=(), question="", notes="", filter_check=None):
    return check_answer(answer, [query(list(rows), filter_check)], question, notes)["unsupported"]


# --- reading figures out of text ---------------------------------------------


@pytest.mark.parametrize(
    ("text", "dates", "numbers"),
    [
        ("The average is 73.75.", [], ["73.75"]),
        ("79.0 over 6 graded enrollments", [], ["79.0", "6"]),
        ("It started on 2026-09-07.", ["2026-09-07"], []),
        ("Fall 2026 has 9 enrollments", [], ["2026", "9"]),
        ("about 35% of 1,250 rows", [], ["35%", "1,250"]),
        ("Grades run from 0 to 100", [], ["0", "100"]),
        ("**86.0** (bold)", [], ["86.0"]),
        # digits that are part of a code, an email or a word are not figures
        ("CS101 and MATH201", [], []),
        ("daniel.kim2@uni.example", [], []),
        ("the 2nd attempt", [], []),
        ("two students", [], []),
        ("No figures here.", [], []),
    ],
)
def test_figures_are_read_as_written(text, dates, numbers):
    assert find_figures(text) == (dates, numbers)


# --- what supports a figure --------------------------------------------------


def test_number_in_a_result_row_is_supported():
    assert unsupported("The average is 73.75.", rows=[[73.75]]) == []


def test_number_not_in_any_result_is_reported():
    assert unsupported("The average is 78.4.", rows=[[73.75]]) == ["78.4"]


@pytest.mark.parametrize(
    ("answer", "cell"),
    [
        ("about 91.33", 91.33333333333333),
        ("about 91.3", 91.33333333333333),
        ("roughly 91", 91.33333333333333),
        ("74.67", 74.66666666666667),
        ("79", 79.0),
        ("79.00", 79),
    ],
)
def test_rounding_to_the_precision_written_is_allowed(answer, cell):
    assert unsupported(answer, rows=[[cell]]) == []


@pytest.mark.parametrize(("answer", "cell"), [("91.4", 91.3333), ("92", 91.3333), ("79.5", 79.0)])
def test_rounding_does_not_stretch_to_a_different_number(answer, cell):
    assert unsupported(answer, rows=[[cell]]) == [answer]


def test_number_inside_a_text_value_is_supported():
    assert unsupported("The latest is Fall 2026.", rows=[["Fall 2026", None]]) == []


def test_date_must_appear_as_a_date_in_the_results():
    assert unsupported("It starts 2026-09-07.", rows=[["Fall 2026", "2026-09-07"]]) == []
    assert unsupported("It starts 2026-09-07.", rows=[["Fall 2026", None]]) == ["2026-09-07"]
    assert unsupported("It starts 2026-09-08.", rows=[["2026-09-07"]]) == ["2026-09-08"]


def test_year_of_a_date_in_the_results_is_supported():
    assert unsupported("It began in 2025.", rows=[["2025-09-01"]]) == []


def test_whole_numbers_up_to_the_row_count_are_supported():
    rows = [["a"], ["b"], ["c"]]
    assert unsupported("There are 3 of them, and 2 are new.", rows=rows) == []
    assert unsupported("There are 4 of them.", rows=rows) == ["4"]
    assert unsupported("It is 2.5 on average.", rows=rows) == ["2.5"]  # not a count


def test_zero_is_a_valid_count_even_for_an_empty_result():
    assert unsupported("0 students matched.", rows=[]) == []


def test_percentage_matches_a_number_or_a_fraction():
    assert unsupported("That is 35%.", rows=[[35.0]]) == []
    assert unsupported("That is 35%.", rows=[[0.35]]) == []
    assert unsupported("That is 35%.", rows=[[11, 31]]) == ["35%"]  # computed, not fetched


def test_figures_from_the_question_are_supported():
    assert unsupported("There is no data for 2024.", question="What happened in 2024?") == []


def test_figures_from_the_schema_notes_are_supported():
    notes = "- Grades are numbers from 0 to 100."
    assert unsupported("No grade is below 0 or above 100.", rows=[["x"]], notes=notes) == []
    assert unsupported("No grade is above 100.", rows=[["x"]]) == ["100"]


def test_figures_from_the_filter_check_are_supported():
    finding = {
        "checked": [
            {
                "filter": "semesters.year = 2024",
                "found": False,
                "detail": "no row has this value; the column holds only: 2025, 2026",
            }
        ],
        "not_checked": 0,
    }
    answer = "There is no 2024 semester. The years are 2025 and 2026."
    assert unsupported(answer, filter_check=finding) == []
    assert unsupported(answer) == ["2024", "2025", "2026"]


def test_figures_from_every_query_in_the_run_count():
    result = check_answer("8 students and 4 teachers.", [query([[8]]), query([[4]])], "", "")
    assert result == {"checked": 2, "unsupported": []}


def test_computed_figures_are_reported_as_not_found():
    """The check looks for figures, it does not redo arithmetic. 79.0 - 70.0 is not fetched."""
    assert unsupported("Markovic is 9.0 points higher.", rows=[[79.0], [70.0]]) == ["9.0"]


def test_each_unsupported_figure_is_listed_once_in_order():
    assert unsupported("First 78.4, then 55.5, then 78.4 again.", rows=[["x"]]) == ["78.4", "55.5"]


def test_true_and_false_cells_are_not_numbers():
    assert unsupported("The value is 1.", rows=[]) == ["1"]
    assert check_answer("It is 7.", [query([[True, 7.0]])], "", "")["unsupported"] == []
    assert check_answer("It is 7.", [query([[True], [False]])], "", "")["unsupported"] == ["7"]


def test_answer_without_figures_checks_nothing():
    assert check_answer("Nobody matched.", [query([])], "", "") == {"checked": 0, "unsupported": []}
