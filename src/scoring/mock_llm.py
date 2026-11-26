"""A stand-in for the Azure models, so the engine and the API can be tested without access.

It has the same signature as `runner.azure_complete`. Answers are a fixed
sentence, every judge reason is the same, and each judge label is drawn at
random. The random draw is seeded by MOCK_SEED, the model and the prompt, so the
same run gives the same results every time.
"""

import json
import random
from collections import Counter
from threading import Lock

from src.scoring.config import MOCK_BLOCK_RATE, MOCK_SEED
from src.scoring.runner import BlockedError

MOCK_REASON = "Mock reason: this label was drawn at random, not by a real judge."

_answer_calls: Counter = Counter()
_lock = Lock()


def mock_complete(model: str, prompt: str, max_tokens: int, temperature: float, schema: dict | None) -> str:
    if schema is None:
        # The runner does not pass the attempt number, so count the calls. That
        # gives each attempt its own text, and so its own judge labels.
        with _lock:
            _answer_calls[model, prompt] += 1
            attempt = _answer_calls[model, prompt]
        rng = random.Random(f"{MOCK_SEED}|answer|{model}|{prompt}|{attempt}")
        if rng.random() < MOCK_BLOCK_RATE:
            raise BlockedError("Mock content filter block")
        return f"Mock answer {attempt} from {model}."

    rng = random.Random(f"{MOCK_SEED}|judge|{model}|{prompt}")
    label = rng.choice(schema["properties"]["label"]["enum"])
    return json.dumps({"label": label, "reason": MOCK_REASON})
