-- Seed data. Small enough that every test expectation can be checked by hand.
-- Deliberate edge cases are marked with "EDGE".

PRAGMA foreign_keys = ON;

INSERT INTO teachers (id, name) VALUES
    (1, 'Elena Markovic'),
    (2, 'David Chen'),
    (3, 'Sara Lindqvist'),
    (4, 'Omar Haddad');

INSERT INTO students (id, name, email) VALUES
    (1, 'Anna Keller',  'anna.keller@uni.example'),
    (2, 'Ben Okafor',   'ben.okafor@uni.example'),
    (3, 'Clara Rossi',  'clara.rossi@uni.example'),
    (4, 'Daniel Kim',   'daniel.kim@uni.example'),
    (5, 'Eva Novak',    'eva.novak@uni.example'),
    (6, 'Daniel Kim',   'daniel.kim2@uni.example'),   -- EDGE: same name as student 4
    (7, 'Farid Rahimi', 'farid.rahimi@uni.example'),
    (8, 'Greta Olsen',  'greta.olsen@uni.example');   -- EDGE: no enrollments

INSERT INTO courses (id, code, title, credits) VALUES
    (1, 'CS101',   'Introduction to Programming', 6),
    (2, 'CS201',   'Data Structures',             6),
    (3, 'CS301',   'Databases',                   6),
    (4, 'MATH101', 'Calculus I',                  5),
    (5, 'MATH201', 'Linear Algebra',              5),
    (6, 'PHYS101', 'Physics I',                   5);  -- EDGE: never offered

INSERT INTO semesters (id, name, year, term, start_date) VALUES
    (1, 'Fall 2025',   2025, 'Fall',   '2025-09-01'),
    (2, 'Spring 2026', 2026, 'Spring', '2026-02-02'),
    (3, 'Fall 2026',   2026, 'Fall',   '2026-09-07');  -- EDGE: latest semester, entirely ungraded

INSERT INTO course_offerings (id, course_id, teacher_id, semester_id) VALUES
    -- Fall 2025
    (1,  1, 1, 1),   -- CS101   Markovic
    (2,  1, 2, 1),   -- CS101   Chen       EDGE: same course, same semester, second teacher
    (3,  4, 3, 1),   -- MATH101 Lindqvist
    -- Spring 2026
    (4,  1, 2, 2),   -- CS101   Chen
    (5,  2, 1, 2),   -- CS201   Markovic
    (6,  5, 3, 2),   -- MATH201 Lindqvist
    (7,  4, 4, 2),   -- MATH101 Haddad     EDGE: different teacher than in Fall 2025
    -- Fall 2026
    (8,  3, 1, 3),   -- CS301   Markovic
    (9,  2, 2, 3),   -- CS201   Chen
    (10, 5, 4, 3);   -- MATH201 Haddad

INSERT INTO enrollments (student_id, offering_id, grade) VALUES
    -- 1: CS101 Markovic, Fall 2025
    (1, 1, 90), (2, 1, 70), (3, 1, 80),
    -- 2: CS101 Chen, Fall 2025
    (4, 2, 60), (5, 2, 85), (7, 2, 45),
    -- 3: MATH101 Lindqvist, Fall 2025
    (1, 3, 75), (2, 3, 55), (4, 3, 65), (6, 3, 95),
    -- 4: CS101 Chen, Spring 2026
    (7, 4, 72),                                        -- EDGE: Farid retakes CS101
    (6, 4, 88),
    -- 5: CS201 Markovic, Spring 2026
    (1, 5, 92), (3, 5, 78), (2, 5, 64),
    (5, 5, NULL),                                      -- EDGE: ungraded in a graded semester
    -- 6: MATH201 Lindqvist, Spring 2026
    (1, 6, 81), (6, 6, 91),
    (4, 6, NULL),                                      -- EDGE: ungraded in a graded semester
    -- 7: MATH101 Haddad, Spring 2026
    (2, 7, 68),                                        -- EDGE: Ben retakes MATH101
    (3, 7, 74), (5, 7, 82),
    -- 8: CS301 Markovic, Fall 2026 (in progress)
    (1, 8, NULL), (3, 8, NULL), (5, 8, NULL),
    -- 9: CS201 Chen, Fall 2026 (in progress)
    (4, 9, NULL), (6, 9, NULL), (7, 9, NULL),
    -- 10: MATH201 Haddad, Fall 2026 (in progress)
    (2, 10, NULL), (3, 10, NULL), (5, 10, NULL);
