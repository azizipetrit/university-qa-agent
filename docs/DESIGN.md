# Design

This document grows with each phase. Phase 1 covers the university database.

## Database

Six tables: `teachers`, `students`, `courses`, `semesters`, `course_offerings`, `enrollments`.

- **Offerings are their own table.** "A course is taught by a teacher in a semester" is a
  three-way fact. Storing it as one row per (course, teacher, semester) lets the same course have
  different teachers in different semesters, or several teachers in the same semester.
- **Enrollments point at offerings, not courses.** A grade belongs to one student in one offering,
  so the teacher and semester of every grade are known. A retake is simply a second enrollment in
  a different offering of the same course.
- **Semesters have a `start_date`.** "Latest semester" is answered by sorting on the date. Sorting
  on the name is wrong ('Spring 2026' sorts after 'Fall 2026'); a test demonstrates this.
- **Grades are numeric 0–100 and NULL while in progress.** SQL aggregates skip NULLs, so averages
  are correct without extra filtering. There is no separate enrollment status column, which would
  be a second source of truth for the same fact.
- **Student names are not unique; emails are.** The seed contains two students named Daniel Kim.
  Grouping by name silently merges them, which is the kind of ambiguity the agent must handle.

The seed data is hand-written and small, so every test expectation can be checked by hand. Its
deliberate edge cases are marked `EDGE` in `university/seed.sql`.

## Assumptions

Small choices made without discussion, simplest option taken.

- Teachers and students have a single `name` column.
- Courses have `code`, `title` and `credits`. There is no departments table.
- Terms are limited to `Spring` and `Fall`.
- Teachers have no email column; only student names collide in the seed.
- Primary keys are integer surrogates. Foreign keys are enforced.
- `build_db.py` uses the standard library `sqlite3` module, because it can run a multi-statement
  SQL script directly. All querying uses SQLAlchemy Core.
- `build_db.py` deletes and recreates the database on every run. The `.db` file is not committed.
- `university` is installed as a second package next to `sqlqa`, so tests can import the builder.
  `sqlqa` never imports it.
