# Examples

Eight runs, each committed as a JSON trace and as rendered Markdown in [`traces/`](traces/).
Seven used the real model (`anthropic:claude-opus-5-5`). One used a scripted model and is labelled.

| # | What it shows | Model calls | Queries | Trace |
| --- | --- | --- | --- | --- |
| 1 | Joins, aggregation and a CTE in one query | 2 | 1 | [01](traces/01-join-and-aggregation.md) |
| 2 | Two people with the same name, reported separately | 2 | 1 | [02](traces/02-duplicate-names.md) |
| 3 | An empty result leads to a second query | 4 | 2 | [03](traces/03-multi-query-after-empty-result.md) |
| 4 | A NULL result is reported as "not available", not zero | 2 | 1 | [04](traces/04-null-result-latest-semester.md) |
| 5 | An ambiguous question gets a clarifying question | 1 | 0 | [05](traces/05-clarification.md) |
| 6 | A request to change data is refused | 1 | 0 | [06](traces/06-refused-write.md) |
| 7 | Rejected SQL is repaired (scripted model) | 3 | 1 | [07](traces/07-repair-scripted.md) |
| 8 | A wrong answer, before it was fixed | 2 | 1 | [08](traces/08-debug-before-fix.md) |

## 1. Joins and aggregation

**Question:** Which teacher has the highest average grade across everything they have taught, and
how many graded enrollments is that based on?

**Answer:** Elena Markovic has the highest average grade, 79.0, across everything they have taught.
That figure is based on 6 graded enrollments. Ungraded (in-progress) enrollments with a NULL grade
were left out, and no other teacher tied for the top average.

```sql
WITH t AS (
  SELECT te.id AS teacher_id, te.name AS teacher_name,
         AVG(e.grade) AS avg_grade, COUNT(e.grade) AS graded_enrollments
  FROM enrollments e
  JOIN course_offerings co ON co.id = e.offering_id
  JOIN teachers te ON te.id = co.teacher_id
  WHERE e.grade IS NOT NULL
  GROUP BY te.id, te.name
)
SELECT teacher_id, teacher_name, avg_grade, graded_enrollments
FROM t WHERE avg_grade = (SELECT MAX(avg_grade) FROM t)
```

The same value (79.0 over 6 grades) is checked by hand in `tests/test_db.py`. The query was laid
out here for reading; the trace has it exactly as the model wrote it.

## 2. Duplicate names

**Question:** What is Daniel Kim's average grade?

**Answer:** There are two students named Daniel Kim. One (daniel.kim@uni.example) averages 62.5
from 2 graded enrollments; the other (daniel.kim2@uni.example) averages about 91.33 from 3.

The model grouped by student id, not by name, so the two students were not merged. A query that
averaged over the name would have returned 79.8, which describes neither of them. The prompt asks
for a unique column whenever a question names a person, and the schema notes say that names repeat.

## 3. A second query after an empty result

**Question:** What is the average grade in the programming intro class taught by Dr. Chen in the
winter term of 2025?

This run had sample rows switched off (`SAMPLE_ROWS=0`), so the model could not see how terms are
written.

1. The first query filtered on a term containing "winter" and returned 0 rows.
2. The `answer` node judged that an empty result was more likely a filter mismatch than a true
   "no such class", and asked for a second query to list this teacher's offerings and the
   semesters that exist. Its reason is recorded in the trace.
3. The second query returned 6 rows.

**Answer:** There is no average to report, because the database has no winter 2025 term. It holds
Fall 2025, Spring 2026 and Fall 2026. David Chen taught CS101 in Fall 2025 and Spring 2026; if you
meant one of these, I can look up its average.

Path: `load_schema → generate_sql → validate_sql → execute_sql → answer → generate_sql →
validate_sql → execute_sql → answer`.

## 4. A NULL result

**Question:** What is the average grade in the latest semester?

**Answer:** The latest semester, by start date, is Fall 2026. Its average grade is not available
yet, because it has 0 graded enrollments. A NULL grade means the enrollment is still in progress,
so there is no average to report (it is not zero).

The query returned one row: `Fall 2026, NULL, 0`. The answer prompt flags NULL results explicitly.

## 5. Clarification

**Question:** Who is the best?

**Answer (status `clarify`):** Who do you mean by "best": the student with the highest average
grade (overall, or in a particular semester or course), or the teacher whose students earn the
highest grades?

The run ends after `generate_sql`. No SQL is written or run.

## 6. A refused write

**Question:** Delete all students.

**Answer (status `unanswerable`):** Deleting students would change the data, and this system can
only run read-only SELECT queries. I can list all students if that helps.

The model declined on its own here. If it had written a `DELETE`, the guard would have rejected it
(`tests/test_agent.py::test_write_statement_is_blocked_before_it_reaches_the_database`), and if the
guard had missed it, the read-only connection would have refused it
(`tests/test_database.py::test_writes_are_refused_by_the_connection_itself`).

## 7. Repair (scripted)

The real model rarely writes SQL that fails on this small schema, so this trace was produced with
the scripted model from `tests/fakes.py`. The trace header says so.

**Question:** How many active students are there?

1. The first query used a column that does not exist: `WHERE active = 1`.
2. The guard passed it, because it checks tables, not columns.
3. The database rejected it: `no such column: active`.
4. The error went back to `generate_sql`, which wrote `SELECT COUNT(*) FROM students`.
5. That query ran, and the answer was 8.

Path: `load_schema → generate_sql → validate_sql → execute_sql → generate_sql → validate_sql →
execute_sql → answer`.

## Debugging a wrong answer

This is a real case from building the project, not a staged one.

**The symptom.** The live evaluation (`uv run pytest -m live`) failed on one question out of 14:

```text
FAILED test_golden_question[What is the average grade in the latest semester?]
values not found in any query result: [None]
answer: The average grade in Spring 2026 is 79.0, based on 10 graded enrollments. This assumes
"latest semester" means the most recent semester (by start date) that has any graded enrollments...
trace: traces/live/20261111-220222-2b7d51.json
```

The latest semester is Fall 2026, which has no grades yet. The expected result was a NULL average.
The agent answered for Spring 2026 instead.

**Reading the trace** ([08-debug-before-fix](traces/08-debug-before-fix.md)), step by step:

| Step | What the trace shows | Verdict |
| --- | --- | --- |
| `load_schema` | The schema and notes are complete. The notes say to order semesters by `start_date`. | Fine |
| `generate_sql` | The plan says: "the latest semester by start_date, **counting only semesters that have at least one graded enrollment**". | The fault is here |
| `validate_sql` | `error: null`. It is a valid SELECT. | Fine |
| `execute_sql` | One row: `Spring 2026, 79.0, 10`. The database did what it was asked. | Fine |
| `answer` | Reports 79.0 for Spring 2026 and states the assumption. | Faithful to the result |

So the SQL was valid, the database was right, and the answer matched the rows. The problem was an
interpretation: the model decided that a semester without grades did not count as "latest". The
plan recorded in the trace says so in the model's own words, which is why `generate_sql` returns a
plan and not only SQL.

**The fix.** This is a fact about the data, so it belongs in the schema notes, not in the agent
code. One line was added to `university/notes.md`:

```text
- The latest semester is the one with the latest start_date, whether or not it has grades yet.
```

**Checking the fix.** The same question now selects Fall 2026 and reports that no average is
available yet ([04-null-result-latest-semester](traces/04-null-result-latest-semester.md)). The
live evaluation covers this question, so a regression would be caught.

### Where to look, by symptom

| Symptom | Look at | Likely cause |
| --- | --- | --- |
| Wrong number, valid SQL | The `plan` in the `generate_sql` reply | The question was read differently than intended |
| Wrong table or join | The schema block at the top of the trace | Missing foreign key, or a missing note |
| 0 rows unexpectedly | The filter values in the SQL against the sample rows | A value written differently from the data |
| Run ended in `give_up` | The `error` in each `validate_sql` and `execute_sql` step | The model could not repair the SQL in 3 attempts |
| Answer disagrees with the rows | The result table in `execute_sql` against the `answer` reply | The answer step misread the result |
| Slow or expensive run | Token counts and durations per model call | A large schema or a long loop |
