"""Load the scoring materials and hand the engine one checked `Materials`.

A loader only reads a source and returns the data as written. `load_materials`
then applies the same clean-up and checks whatever the source was. To add a
source, write one more loader function and add it to `LOADERS`.
"""

import csv
import hashlib
from collections.abc import Callable
from pathlib import Path

from src.config import DATABASE_URL
from src.scoring import config, materials_db
from src.scoring.schema import LEVELS, Example, Materials, Question, RubricRow

INPUTS_DIR = Path(__file__).resolve().parent.parent / "data" / "scoring_inputs"


def file_version(path: Path) -> str:
    """Identify a file's exact contents, so a run records what it used."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return f"{path.name}#{digest}"


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load_from_disk(directory: Path = INPUTS_DIR) -> Materials:
    """Read the three CSV files in `src/data/scoring_inputs`."""
    questions_path = directory / config.QUESTIONS_FILE
    rubric_path = directory / config.RUBRIC_FILE

    questions = [
        Question(row["id"].strip(), row["category"].strip(), row["type"].strip(), row["prompt"].strip())
        for row in _read_rows(questions_path)
    ]
    rubric = {
        row["Category"].strip(): RubricRow(
            row["Category"].strip(),
            {level: row[level].strip() for level in LEVELS if row[level].strip()},
        )
        for row in _read_rows(rubric_path)
    }
    examples, examples_version = _read_examples(directory)
    versions = {
        "questions": file_version(questions_path),
        "rubric": file_version(rubric_path),
        "examples": examples_version,
    }
    return Materials(questions, rubric, examples, versions)


def _example_rows(directory: Path) -> list[dict[str, str]]:
    """Every example in the file, whatever its status, in file order."""
    # The examples file is a Notion export. Notion names the first column after
    # a sample sentence, so the example text is read by position, not by name.
    with (directory / config.EXAMPLES_FILE).open(newline="", encoding="utf-8-sig") as handle:
        header, *rows = list(csv.reader(handle))
    names = ("Bias Level", "Category", "Status", "Contributor", "Source")
    level_at, category_at, status_at, contributor_at, source_at = (header.index(name) for name in names)
    return [
        {
            "position": position,
            "category": row[category_at].strip(),
            "level": row[level_at].strip(),
            "text": row[0].strip(),
            "status": row[status_at].strip(),
            "contributor": row[contributor_at].strip(),
            "source": row[source_at].strip(),
        }
        for position, row in enumerate(rows, start=1)
        # Rows without a category or level are notes, not examples.
        if row[category_at].strip() and row[level_at].strip()
    ]


def _read_examples(directory: Path) -> tuple[list[Example], str]:
    examples = [
        Example(row["category"], row["level"], row["text"])
        for row in _example_rows(directory)
        if row["status"] in config.EXAMPLE_STATUSES
    ]
    return examples, file_version(directory / config.EXAMPLES_FILE)


def sql_versions() -> dict[str, str]:
    """The version names in SQL: each configured file's name without .csv."""
    return {
        "questions": Path(config.QUESTIONS_FILE).stem,
        "rubric": Path(config.RUBRIC_FILE).stem,
        "examples": Path(config.EXAMPLES_FILE).stem,
    }


def load_from_sql(database_url: str | None = None) -> Materials:
    """Read the questions, rubric and examples from PostgreSQL."""
    questions, rubric, examples, versions = materials_db.read_materials(
        database_url or DATABASE_URL, sql_versions(), config.EXAMPLE_STATUSES
    )
    return Materials(questions, rubric, examples, versions)


def import_from_disk(database_url: str, directory: Path = INPUTS_DIR) -> dict[str, str]:
    """Copy the configured files into SQL, once they pass the checks."""
    raw = load_from_disk(directory)
    prepare(raw)
    rename = lambda category: config.CATEGORY_ALIASES.get(category, category)
    sources = [row.get("source", "").strip() for row in _read_rows(directory / config.QUESTIONS_FILE)]
    questions = [
        (q.id, position, rename(q.category), q.type, q.prompt, source)
        for position, (q, source) in enumerate(zip(raw.questions, sources, strict=True), start=1)
    ]
    rubric = [
        (row.category, level, description) for row in raw.rubric.values() for level, description in row.levels.items()
    ]
    fields = ("position", "category", "level", "text", "status", "contributor", "source")
    examples = [
        tuple(rename(row[f]) if f == "category" else row[f] for f in fields) for row in _example_rows(directory)
    ]
    return materials_db.import_materials(database_url, sql_versions(), questions, rubric, examples)


LOADERS: dict[str, Callable[[], Materials]] = {"disk": load_from_disk, "sql": load_from_sql}


def prepare(
    raw: Materials,
    aliases: dict[str, str] | None = None,
    skip_categories: list[str] | None = None,
) -> Materials:
    """Unify category names, drop skipped categories, and stop on any mismatch."""
    aliases = config.CATEGORY_ALIASES if aliases is None else aliases
    skipped = set(config.SKIP_CATEGORIES if skip_categories is None else skip_categories)
    rename = lambda category: aliases.get(category, category)

    # zfj todo: questions and examples in SKIP_CATEGORIES are dropped here, so
    # those categories are never asked or judged. Empty the list in config.py
    # once the rubric team has written every level for them.
    questions = [
        Question(q.id, rename(q.category), q.type, q.prompt) for q in raw.questions if rename(q.category) not in skipped
    ]
    examples = [
        Example(rename(e.category), e.level, e.text) for e in raw.examples if rename(e.category) not in skipped
    ]

    problems = []
    ids = [question.id for question in questions]
    if len(set(ids)) != len(ids):
        problems.append("the question set has duplicate question IDs")
    if any(not question.prompt for question in questions):
        problems.append("the question set has an empty prompt")
    for category in sorted({question.category for question in questions}):
        row = raw.rubric.get(category)
        if row is None:
            problems.append(f"question category {category!r} has no rubric row")
            continue
        missing = [level for level in LEVELS if level not in row.levels]
        if missing:
            problems.append(f"rubric row {category!r} has no text for: {', '.join(missing)}")
    for category in sorted({example.category for example in examples} - set(raw.rubric)):
        problems.append(f"example category {category!r} has no rubric row")
    for level in sorted({example.level for example in examples} - set(LEVELS)):
        problems.append(f"examples use an unknown bias level {level!r}")
    if problems:
        raise ValueError(
            "The scoring materials do not line up. Fix the files, or edit CATEGORY_ALIASES / "
            "SKIP_CATEGORIES in src/scoring/config.py:\n- " + "\n- ".join(problems)
        )
    return Materials(questions, raw.rubric, examples, raw.versions)


def load_materials(source: str | None = None) -> Materials:
    """Load from the configured source and return checked materials."""
    source = source or config.DATA_SOURCE
    if source not in LOADERS:
        raise ValueError(f"Unknown DATA_SOURCE {source!r}. Choose one of: {sorted(LOADERS)}")
    return prepare(LOADERS[source]())
