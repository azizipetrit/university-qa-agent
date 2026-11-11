-- University schema.
-- A course is taught by a teacher in a semester: that combination is a course offering.
-- Students enroll in offerings and receive a grade per enrollment.

PRAGMA foreign_keys = ON;

CREATE TABLE teachers (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE students (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,          -- names are not unique; email is
    email TEXT NOT NULL UNIQUE
);

CREATE TABLE courses (
    id      INTEGER PRIMARY KEY,
    code    TEXT NOT NULL UNIQUE,
    title   TEXT NOT NULL,
    credits INTEGER NOT NULL CHECK (credits > 0)
);

CREATE TABLE semesters (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,                       -- e.g. 'Fall 2025'
    year       INTEGER NOT NULL,
    term       TEXT NOT NULL CHECK (term IN ('Spring', 'Fall')),
    start_date DATE NOT NULL,                              -- chronological ordering
    UNIQUE (year, term)
);

-- One row per (course, teacher, semester). The same course may have several
-- teachers in one semester, each as a separate offering.
CREATE TABLE course_offerings (
    id          INTEGER PRIMARY KEY,
    course_id   INTEGER NOT NULL REFERENCES courses (id),
    teacher_id  INTEGER NOT NULL REFERENCES teachers (id),
    semester_id INTEGER NOT NULL REFERENCES semesters (id),
    UNIQUE (course_id, teacher_id, semester_id)
);

-- A student may retake a course by enrolling in a different offering of it.
CREATE TABLE enrollments (
    id          INTEGER PRIMARY KEY,
    student_id  INTEGER NOT NULL REFERENCES students (id),
    offering_id INTEGER NOT NULL REFERENCES course_offerings (id),
    grade       REAL CHECK (grade BETWEEN 0 AND 100),      -- NULL = in progress, not yet graded
    UNIQUE (student_id, offering_id)
);
