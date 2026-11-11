import sqlite3

import pytest
from sqlalchemy import create_engine, event

from sqlqa.db import Database
from university.build_db import HERE as UNIVERSITY_DIR
from university.build_db import build_database


@pytest.fixture(scope="session")
def university_db_path(tmp_path_factory):
    """A freshly built copy of the university database, shared by read-only tests."""
    return build_database(tmp_path_factory.mktemp("db") / "university.db")


@pytest.fixture
def conn(university_db_path):
    """A writable connection whose changes are rolled back (used to test constraints)."""
    engine = create_engine(f"sqlite:///{university_db_path.as_posix()}")

    @event.listens_for(engine, "connect")
    def enforce_foreign_keys(dbapi_connection, _record):
        # SQLite only enforces foreign keys when asked, per connection.
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    with engine.connect() as connection:
        yield connection
        connection.rollback()
    engine.dispose()


@pytest.fixture
def db(university_db_path):
    """The agent's view of the university database."""
    database = Database(
        f"sqlite:///{university_db_path.as_posix()}",
        notes_file=UNIVERSITY_DIR / "notes.md",
    )
    yield database
    database.engine.dispose()


@pytest.fixture
def bookstore_db(tmp_path):
    """An unrelated schema, to show that nothing depends on the university tables."""
    path = tmp_path / "bookstore.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE books (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            price NUMERIC(6, 2),
            published DATE,
            author_id INTEGER NOT NULL REFERENCES authors (id)
        );
        CREATE VIEW cheap_books AS SELECT * FROM books WHERE price < 10;
        INSERT INTO authors VALUES (1, 'Ursula Le Guin'), (2, 'Italo Calvino');
        INSERT INTO books VALUES
            (1, 'The Dispossessed', 12.50, '1974-05-01', 1),
            (2, 'Invisible Cities', 9.00, '1972-11-03', 2);
        """
    )
    connection.commit()
    connection.close()
    database = Database(f"sqlite:///{path.as_posix()}")
    yield database
    database.engine.dispose()
