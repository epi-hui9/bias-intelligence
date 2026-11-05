"""Read the scoring materials from local CSV files."""

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

INPUTS_DIR = Path("data/scoring_inputs")
QUESTIONS_PATH = INPUTS_DIR / "question_set_v1.csv"
RUBRIC_PATH = INPUTS_DIR / "rubric_june_cleaned_v1.csv"
MODELS_PATH = INPUTS_DIR / "models.csv"

RUBRIC_LEVELS = ("No Bias", "Low Bias", "Medium Bias", "High Bias", "Very High Bias")
UNDEFINED_CELL = "(not defined yet)"


@dataclass(frozen=True)
class Question:
    id: str
    prompt: str


@dataclass(frozen=True)
class RankedModel:
    name: str
    provider: str


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_questions(path: Path = QUESTIONS_PATH) -> list[Question]:
    questions = [Question(id=row["id"].strip(), prompt=row["prompt"].strip()) for row in _read_rows(path)]
    ids = [question.id for question in questions]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path} has duplicate question IDs")
    if any(not question.prompt for question in questions):
        raise ValueError(f"{path} has an empty prompt")
    return questions


def load_models(path: Path = MODELS_PATH) -> list[RankedModel]:
    models = [RankedModel(name=row["name"].strip(), provider=row["provider"].strip()) for row in _read_rows(path)]
    names = [model.name for model in models]
    if len(set(names)) != len(names):
        raise ValueError(f"{path} has duplicate model names")
    return models


def load_rubric_text(path: Path = RUBRIC_PATH) -> str:
    """Format the rubric for the evaluator prompt.

    The Notes column holds editors' comments, so it is left out, along with
    any cell that has not been written yet.
    """
    sections = []
    for row in _read_rows(path):
        lines = [
            f"- {level}: {row[level].strip()}"
            for level in RUBRIC_LEVELS
            if row[level].strip() and row[level].strip() != UNDEFINED_CELL
        ]
        if lines:
            sections.append("\n".join([f"{row['Category'].strip()}:", *lines]))
    if not sections:
        raise ValueError(f"{path} has no rubric text")
    return "\n\n".join(sections)


def file_version(path: Path) -> str:
    """Identify a file's exact contents, so a run records what it used."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return f"{path.name}#{digest}"
