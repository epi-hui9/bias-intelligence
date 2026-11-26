"""The question set, rubric and rubric examples in PostgreSQL.

Rows are stored as the files have them, except that category names are saved
in the rubric's spelling (CATEGORY_ALIASES). The checks run when they are
loaded, in loaders.py. A saved version is never changed.
"""

import hashlib
import json

import psycopg

from src.scoring.schema import LEVELS, Example, Question, RubricRow

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    version TEXT NOT NULL,
    id TEXT NOT NULL,
    position INTEGER NOT NULL,
    category TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL CHECK (prompt <> ''),
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (version, id),
    UNIQUE (version, position)
);

CREATE TABLE IF NOT EXISTS rubrics (
    version TEXT NOT NULL,
    category TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('No Bias', 'Low Bias', 'Medium Bias', 'High Bias', 'Very High Bias')),
    description TEXT NOT NULL CHECK (description <> ''),
    PRIMARY KEY (version, category, level)
);

-- Every example in the file, whatever its status. A run uses only EXAMPLE_STATUSES.
CREATE TABLE IF NOT EXISTS rubric_examples (
    version TEXT NOT NULL,
    position INTEGER NOT NULL,
    category TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('No Bias', 'Low Bias', 'Medium Bias', 'High Bias', 'Very High Bias')),
    text TEXT NOT NULL CHECK (text <> ''),
    status TEXT NOT NULL DEFAULT '',
    contributor TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (version, position)
);
"""

SELECT_QUESTIONS = (
    "SELECT id, position, category, type, prompt, source FROM questions WHERE version = %s ORDER BY position"
)
SELECT_RUBRIC = "SELECT category, level, description FROM rubrics WHERE version = %s"
SELECT_EXAMPLES = (
    "SELECT position, category, level, text, status, contributor, source FROM rubric_examples "
    "WHERE version = %s ORDER BY position"
)


def _connect(database_url: str | None):
    if not database_url:
        raise ValueError("DATABASE_URL must be set")
    return psycopg.connect(database_url)


def _version(name: str, rows: list[tuple]) -> str:
    """Identify the exact rows a run read, like loaders.file_version does for a file."""
    digest = hashlib.sha256(json.dumps(sorted(rows), ensure_ascii=False).encode()).hexdigest()[:12]
    return f"{name}#{digest}"


def import_materials(
    database_url: str,
    versions: dict[str, str],
    questions: list[tuple],
    rubric: list[tuple],
    examples: list[tuple],
) -> dict[str, str]:
    """Create the three tables and add each version, in one transaction. Return what happened to each."""
    with _connect(database_url) as connection:
        connection.execute(SCHEMA)
        return {
            "questions": _add(
                connection, "questions", versions["questions"], questions, SELECT_QUESTIONS,
                "INSERT INTO questions (version, id, position, category, type, prompt, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            ),
            "rubrics": _add(
                connection, "rubrics", versions["rubric"], rubric, SELECT_RUBRIC,
                "INSERT INTO rubrics (version, category, level, description) VALUES (%s, %s, %s, %s)",
            ),
            "rubric_examples": _add(
                connection, "rubric_examples", versions["examples"], examples, SELECT_EXAMPLES,
                "INSERT INTO rubric_examples (version, position, category, level, text, status, contributor, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            ),
        }


def _add(connection, table: str, version: str, rows: list[tuple], select: str, insert: str) -> str:
    saved = connection.execute(select, (version,)).fetchall()
    if saved:
        if sorted(saved) != sorted(rows):
            raise ValueError(
                f"{table} already has a different {version!r}. Give the changed file a new name "
                "and set it in src/scoring/config.py."
            )
        return f"{version}: already saved, {len(rows)} rows"
    connection.cursor().executemany(insert, [(version, *row) for row in rows])
    return f"{version}: added {len(rows)} rows"


def read_materials(
    database_url: str | None, versions: dict[str, str], example_statuses: list[str]
) -> tuple[list[Question], dict[str, RubricRow], list[Example], dict[str, str]]:
    """Return one version of each: questions and examples in file order, and the rubric."""
    try:
        with _connect(database_url) as connection:
            question_rows = connection.execute(SELECT_QUESTIONS, (versions["questions"],)).fetchall()
            rubric_rows = connection.execute(SELECT_RUBRIC, (versions["rubric"],)).fetchall()
            example_rows = connection.execute(SELECT_EXAMPLES, (versions["examples"],)).fetchall()
    except psycopg.errors.UndefinedTable as exc:
        raise ValueError("The materials tables do not exist. Run: python -m src.scoring import-materials") from exc
    for table, version, rows in (
        ("questions", versions["questions"], question_rows),
        ("rubrics", versions["rubric"], rubric_rows),
        ("rubric_examples", versions["examples"], example_rows),
    ):
        if not rows:
            raise ValueError(f"{table} has no {version!r}. Run: python -m src.scoring import-materials")

    questions = [Question(id_, category, type_, prompt) for id_, _, category, type_, prompt, _ in question_rows]
    texts: dict[str, dict[str, str]] = {}
    for category, level, description in rubric_rows:
        texts.setdefault(category, {})[level] = description
    rubric = {
        category: RubricRow(category, {level: levels[level] for level in LEVELS if level in levels})
        for category, levels in texts.items()
    }
    # The version covers every row, so a status change made in SQL shows up too.
    examples = [Example(category, level, text) for _, category, level, text, status, *_ in example_rows
                if status in example_statuses]
    read_versions = {
        "questions": _version(versions["questions"], question_rows),
        "rubric": _version(versions["rubric"], rubric_rows),
        "examples": _version(versions["examples"], example_rows),
    }
    return questions, rubric, examples, read_versions
