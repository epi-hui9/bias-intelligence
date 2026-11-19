"""The fixed data shapes the scoring engine works with.

Loaders turn files (or, later, Azure storage) into these classes. Everything
after loading uses only these classes, so swapping the data source never
touches the engine.
"""

from dataclasses import dataclass

LEVELS = ("No Bias", "Low Bias", "Medium Bias", "High Bias", "Very High Bias")


@dataclass(frozen=True)
class Model:
    """A model to rank. The same model at another effort setting is another entry."""

    name: str
    provider: str
    effort: str = "default"


@dataclass(frozen=True)
class Question:
    id: str
    category: str
    type: str
    prompt: str


@dataclass(frozen=True)
class RubricRow:
    """One category's rubric: the description of each level that has been written."""

    category: str
    levels: dict[str, str]


@dataclass(frozen=True)
class Example:
    """A sentence the rubric team labeled, used as a few-shot example for judges."""

    category: str
    level: str
    text: str


@dataclass(frozen=True)
class Materials:
    """Everything a run reads, with a version string for each source."""

    questions: list[Question]
    rubric: dict[str, RubricRow]
    examples: list[Example]
    versions: dict[str, str]


@dataclass(frozen=True)
class Judgement:
    """One judge call's verdict on one answer."""

    judge: str
    order: str
    level: str
    reason: str = ""
