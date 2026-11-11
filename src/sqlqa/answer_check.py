"""After the answer is written: is every figure in it backed by something the run fetched?

The model is told to use only the query results, but nothing forced it to. A figure can
leak in from elsewhere, for example from the sample rows shown with the schema. This check
finds the numbers and dates in the answer and looks for each one in the places it may
legitimately come from:

- the rows of every query in the run,
- what the filter check reported,
- the question itself,
- the schema notes (documentation the answer is allowed to quote).

It checks presence, not arithmetic. A figure the model computed, such as a difference
between two results, is reported as not found. It is not said to be wrong.
"""

import re

DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# A number standing on its own: not part of a word, a code or an email (CS101, kim2@...).
NUMBER = re.compile(r"(?<![A-Za-z0-9_.@])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?![A-Za-z0-9_@])")


def find_figures(text: str) -> tuple[list[str], list[str]]:
    """The dates and the numbers in `text`, as written, in order."""
    dates = DATE.findall(text)
    numbers = NUMBER.findall(DATE.sub(" ", text))
    return dates, numbers


def check_answer(answer: str, queries: list[dict], question: str, notes: str) -> dict:
    """Which figures in `answer` were checked, and which were found nowhere."""
    source_numbers: list[float] = []
    source_dates: set[str] = set()

    def add_text(text: str):
        dates, numbers = find_figures(text)
        source_dates.update(dates)
        source_numbers.extend(float(date[:4]) for date in dates)  # the year of a date
        source_numbers.extend(_value(number) for number in numbers)

    add_text(question)
    add_text(notes)
    most_rows = 0
    for query in queries:
        most_rows = max(most_rows, len(query["rows"]))
        for row in query["rows"]:
            for cell in row:
                if isinstance(cell, bool) or cell is None:
                    continue
                if isinstance(cell, int | float):
                    source_numbers.append(float(cell))
                else:
                    add_text(str(cell))
        for item in (query.get("filter_check") or {}).get("checked", []):
            add_text(f"{item['filter']} {item['detail']}")

    dates, numbers = find_figures(answer)
    unsupported = [date for date in dates if date not in source_dates]
    for number in numbers:
        value, decimals = _value(number), _decimals(number)
        candidates = [value, value / 100] if number.endswith("%") else [value]
        # Rounding: a source value that rounds to this figure at the precision it is written in.
        tolerance = 0.5 * 10**-decimals + 1e-9
        in_sources = any(
            abs(source - candidate) <= tolerance
            for candidate in candidates
            for source in source_numbers
        )
        # Row counts: a whole number no larger than a result's row count may be a count of rows.
        is_row_count = decimals == 0 and not number.endswith("%") and value <= most_rows
        if not (in_sources or is_row_count):
            unsupported.append(number)

    return {
        "checked": len(dates) + len(numbers),
        "unsupported": list(dict.fromkeys(unsupported)),  # each figure once, in order
    }


def _value(number: str) -> float:
    return float(number.rstrip("%").replace(",", ""))


def _decimals(number: str) -> int:
    digits = number.rstrip("%")
    return len(digits.split(".")[1]) if "." in digits else 0
