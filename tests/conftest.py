import pytest
from sqlalchemy import create_engine, event

from university.build_db import build_database


@pytest.fixture(scope="session")
def university_db_path(tmp_path_factory):
    """A freshly built copy of the university database, shared by read-only tests."""
    return build_database(tmp_path_factory.mktemp("db") / "university.db")


@pytest.fixture
def conn(university_db_path):
    """A connection whose changes are rolled back, so tests cannot affect each other."""
    engine = create_engine(f"sqlite:///{university_db_path.as_posix()}")

    @event.listens_for(engine, "connect")
    def enforce_foreign_keys(dbapi_connection, _record):
        # SQLite only enforces foreign keys when asked, per connection.
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    with engine.connect() as connection:
        yield connection
        connection.rollback()
    engine.dispose()
