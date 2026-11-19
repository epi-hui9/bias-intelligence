import os
import re
import unittest
from dataclasses import replace
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient

from src.api import fastapi_app
from src.api.fastapi_app import app
from src.llm.azure_openai_client import ModelCompletionError
from src.scoring import runner
from src.scoring.config import CANDIDATE_MODELS, JUDGE_MODELS, LETTERED_ORDER, ScoringConfig
from src.scoring.judge import build_prompt, parse_judgement
from src.scoring.loaders import load_materials, prepare
from src.scoring.merge import AnswerOutcome, merge, rank_models
from src.scoring.runner import BlockedError, RefusedError, ask_models, judge_answers, with_retries
from src.scoring.schema import LEVELS, Example, Judgement, Materials, Model, Question, RubricRow
from src.scoring.store import ScoringStore

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


def small_materials(question_categories=("Cat", "Cat"), rubric_levels=LEVELS, examples=None) -> Materials:
    questions = [
        Question(f"Q{number}", category, "neutral", f"Question {number}?")
        for number, category in enumerate(question_categories, start=1)
    ]
    rubric = {"Cat": RubricRow("Cat", {level: f"DESC {level}" for level in rubric_levels})}
    if examples is None:
        examples = [Example("Cat", level, f"EXAMPLE {level} {n}") for level in LEVELS for n in (1, 2, 3)]
    return Materials(questions, rubric, examples, {"questions": "q#1", "rubric": "r#1", "examples": "e#1"})


class InputTests(unittest.TestCase):
    def test_saved_materials_line_up_and_use_the_rubric_spelling(self):
        materials = load_materials()
        categories = {question.category for question in materials.questions}
        self.assertEqual(len(materials.questions), 80)
        self.assertEqual(len({question.id for question in materials.questions}), 80)
        self.assertIn("Science, Technology, & Medicine", categories)
        self.assertIn("Racial Equity", categories)
        self.assertNotIn("Education", categories)
        self.assertNotIn("Disability", categories)
        self.assertLessEqual({example.category for example in materials.examples}, categories)
        self.assertNotIn("June’s Original Rubric", {example.text for example in materials.examples})

    def test_aliases_rename_categories_in_questions_and_examples(self):
        raw = small_materials(("cat", "Cat"), examples=[Example("CAT", "No Bias", "x")])
        materials = prepare(raw, aliases={"cat": "Cat", "CAT": "Cat"}, skip_categories=[])
        self.assertEqual({question.category for question in materials.questions}, {"Cat"})
        self.assertEqual(materials.examples[0].category, "Cat")

    def test_mismatched_materials_stop_the_run(self):
        cases = {
            "has no rubric row": small_materials(("Cat", "Other")),
            "has no text for: Medium Bias": small_materials(rubric_levels=[l for l in LEVELS if l != "Medium Bias"]),
            "example category 'Other' has no rubric row": small_materials(examples=[Example("Other", "No Bias", "x")]),
            "unknown bias level": small_materials(examples=[Example("Cat", "Some Bias", "x")]),
        }
        for message, raw in cases.items():
            with self.assertRaisesRegex(ValueError, message):
                prepare(raw, aliases={}, skip_categories=[])

    def test_skipped_categories_are_dropped_instead_of_failing(self):
        raw = small_materials(("Cat", "Other"), examples=[Example("Other", "No Bias", "x")])
        materials = prepare(raw, aliases={}, skip_categories=["Other"])
        self.assertEqual([question.id for question in materials.questions], ["Q1"])
        self.assertEqual(materials.examples, [])

    def test_config_has_20_models_and_3_judges(self):
        self.assertEqual(len({model.name for model in CANDIDATE_MODELS}), 20)
        self.assertEqual(len(set(JUDGE_MODELS)), 3)
        self.assertEqual(sorted(LETTERED_ORDER), sorted(LEVELS))


class JudgeTests(unittest.TestCase):
    def prompt(self, order, answer="An answer", few_shots=2):
        materials = small_materials()
        return build_prompt(
            materials.questions[0], answer, materials.rubric["Cat"], materials.examples, order, few_shots, LETTERED_ORDER
        )

    def test_the_three_orders_lay_the_levels_out_differently(self):
        for order, expected in (("ascending", LEVELS), ("descending", LEVELS[::-1]), ("lettered", LETTERED_ORDER)):
            prompt = self.prompt(order)
            positions = [prompt.text.index(f"DESC {level}") for level in expected]
            self.assertEqual(positions, sorted(positions), order)
            self.assertEqual(list(prompt.labels.values()), list(expected))

    def test_lettered_order_hides_level_names_behind_letters(self):
        prompt = self.prompt("lettered")
        self.assertEqual(list(prompt.labels), ["A", "B", "C", "D", "E"])
        self.assertNotIn("[No Bias]", prompt.text)
        self.assertEqual(prompt.schema["properties"]["label"]["enum"], ["A", "B", "C", "D", "E"])

    def test_prompt_uses_only_the_question_category_and_limits_examples(self):
        materials = small_materials()
        examples = materials.examples + [Example("Other", "No Bias", "OTHER CATEGORY EXAMPLE")]
        prompt = build_prompt(materials.questions[0], "A {braces} answer", materials.rubric["Cat"], examples, "ascending", 2)
        self.assertIn('category "Cat"', prompt.text)
        self.assertIn("EXAMPLE High Bias 2", prompt.text)
        self.assertNotIn("EXAMPLE High Bias 3", prompt.text)
        self.assertNotIn("OTHER CATEGORY EXAMPLE", prompt.text)
        self.assertIn("QUESTION: Question 1?", prompt.text)
        self.assertIn("ANSWER: A {braces} answer", prompt.text)

    def test_unknown_order_is_rejected(self):
        with self.assertRaises(ValueError):
            self.prompt("sideways")

    def test_parse_maps_the_label_back_to_a_level(self):
        lettered = self.prompt("lettered").labels
        self.assertEqual(parse_judgement('```json\n{"label": "C", "reason": "Why."}\n```', lettered), (LETTERED_ORDER[2], "Why."))
        named = self.prompt("ascending").labels
        self.assertEqual(parse_judgement('{"label": "low bias", "reason": "Minor."}', named), ("Low Bias", "Minor."))

    def test_parse_rejects_bad_output(self):
        labels = self.prompt("lettered").labels
        for output in ("no json", "{not json}", '{"label": "F", "reason": "x"}', '{"label": "No Bias", "reason": "x"}', '{"label": "A", "reason": ""}'):
            with self.assertRaises(ValueError, msg=output):
                parse_judgement(output, labels)


def votes(**by_judge) -> list[Judgement]:
    orders = ("ascending", "descending", "lettered")
    return [Judgement(judge, order, level) for judge, levels in by_judge.items() for order, level in zip(orders, levels)]


class MergeTests(unittest.TestCase):
    def test_median_of_medians_ignores_one_odd_order_and_one_odd_judge(self):
        judgements = votes(
            a=("Low Bias", "Low Bias", "Very High Bias"),
            b=("Low Bias", "Medium Bias", "Low Bias"),
            c=("Very High Bias", "Very High Bias", "Very High Bias"),
        )
        self.assertEqual(merge(judgements, "median_of_medians"), "Low Bias")

    def test_two_judges_that_disagree_give_the_more_biased_label(self):
        judgements = votes(a=("Low Bias",) * 3, b=("High Bias",) * 3)
        self.assertEqual(merge(judgements, "median_of_medians"), "High Bias")

    def test_majority_vote_is_a_different_method(self):
        judgements = votes(
            a=("No Bias", "No Bias", "High Bias"),
            b=("No Bias", "High Bias", "High Bias"),
            c=("High Bias", "High Bias", "No Bias"),
        )
        self.assertEqual(merge(judgements, "majority_vote"), "High Bias")
        self.assertEqual(merge(votes(a=("No Bias", "No Bias", "High Bias")), "majority_vote"), "No Bias")

    def test_no_judgements_and_unknown_methods(self):
        self.assertIsNone(merge([], "median_of_medians"))
        with self.assertRaises(ValueError):
            merge(votes(a=("No Bias",) * 3), "coin_flip")

    def test_ranking_averages_questions_then_the_model_and_shares_tied_ranks(self):
        outcomes = [
            # Q1 averages 0.25 and Q2 is 0.75, so model-a is 0.5.
            AnswerOutcome("model-a", "Q1", "No Bias"),
            AnswerOutcome("model-a", "Q1", "Low Bias"),
            AnswerOutcome("model-a", "Q1", "Medium Bias"),
            AnswerOutcome("model-a", "Q2", "High Bias"),
            AnswerOutcome("model-a", "Q2", None, blocked=True),
            AnswerOutcome("model-a", "Q2", None),
            AnswerOutcome("model-b", "Q1", "Medium Bias"),
            AnswerOutcome("model-c", "Q1", "Very High Bias"),
            AnswerOutcome("model-d", "Q1", None),
        ]
        rankings = rank_models(outcomes)
        self.assertEqual([(row["model"], row["rank"], row["score"]) for row in rankings],
                         [("model-c", 1, 1.0), ("model-a", 2, 0.5), ("model-b", 2, 0.5)])
        self.assertEqual({key: rankings[1][key] for key in ("scored", "blocked", "failed")},
                         {"scored": 4, "blocked": 1, "failed": 1})


class RetryTests(unittest.TestCase):
    def test_temporary_errors_retry_three_times_then_raise(self):
        calls = []

        def fail():
            calls.append(1)
            raise RuntimeError("timeout")

        with self.assertRaises(RuntimeError):
            with_retries(fail, base_seconds=0)
        self.assertEqual(len(calls), 4)

    def test_recovers_when_a_retry_works(self):
        results = iter([RuntimeError("timeout"), "ok"])

        def call():
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result

        self.assertEqual(with_retries(call, base_seconds=0), "ok")

    def test_blocked_and_refused_calls_are_not_retried(self):
        for error in (BlockedError, RefusedError):
            calls = []

            def fail():
                calls.append(1)
                raise error("no")

            with self.assertRaises(error):
                with_retries(fail, base_seconds=0)
            self.assertEqual(len(calls), 1)

    def test_azure_errors_map_to_blocked_and_refused(self):
        cases = [
            (ModelCompletionError("x", code="content_filter_blocked"), BlockedError),
            (ModelCompletionError("provider said content_filter", code="completion_failed"), BlockedError),
            (ModelCompletionError("x", code="model_refusal"), RefusedError),
            (ModelCompletionError("x", code="completion_failed"), ModelCompletionError),
        ]
        for raised, expected in cases:
            with patch.object(runner, "openai_client"), patch.object(runner, "create_completion", side_effect=raised):
                with self.assertRaises(expected):
                    runner.azure_complete("gpt-5.5", "prompt", 10, 0.0, None)


class RankingsRouteWithoutDatabaseTests(unittest.TestCase):
    def test_missing_database_setting_returns_503(self):
        with patch.object(fastapi_app, "DATABASE_URL", None):
            response = TestClient(app).get("/api/rankings")
        self.assertEqual(response.status_code, 503)


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to run PostgreSQL tests")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        with psycopg.connect(TEST_DATABASE_URL) as connection:
            connection.execute("DROP TABLE IF EXISTS model_scores, answer_labels, judgements, answers, models, runs")
        self.store = ScoringStore(TEST_DATABASE_URL)
        self.store.init_schema()
        self.models = [Model("gpt-5.5", "OpenAI"), Model("model-b", "B"), Model("model-c", "C")]
        self.materials = small_materials()
        self.config = ScoringConfig(
            judge_models=("gpt-5.5", "judge-b", "judge-c"), models=tuple(self.models), workers=4, retry_base_seconds=0
        )
        self.levels = {"gpt-5.5": "Medium Bias", "model-b": "High Bias", "model-c": "Low Bias"}
        self.judge_calls = []
        self.answer_calls = 0
        self.fail_answers_for = set()
        self.block_answers_for = set()
        self.refuse_answers_for = set()
        self.fail_judging_for = set()
        self.judge_overrides = {}

    def complete(self, model, prompt, max_tokens, temperature, schema):
        if schema is None:
            self.answer_calls += 1
            if model in self.fail_answers_for:
                raise RuntimeError("timeout")
            if model in self.block_answers_for and prompt == "Question 2?":
                raise BlockedError("content_filter")
            if model in self.refuse_answers_for:
                raise RefusedError("declined")
            return f"Answer from {model}"
        answerer = next((name for name in self.levels if f"ANSWER: Answer from {name}" in prompt), "refused")
        self.judge_calls.append((model, answerer))
        if answerer in self.fail_judging_for:
            return "not json"
        level = self.judge_overrides.get(model) or self.levels.get(answerer, "No Bias")
        # Answer with whatever label this prompt layout gives the level.
        label = re.search(rf"\[([^\]]+)\]\nDescription: DESC {level}\n", prompt).group(1)
        return f'{{"label": "{label}", "reason": "Because."}}'

    def calculate(self, run_id, config=None):
        config = config or self.config
        self.store.calculate(run_id, config.merge_method, config.judge_models, config.judge_orders)

    def run_engine(self, models=None, config=None):
        config = config or self.config
        run_id = self.store.create_run("questions#1", config.snapshot())
        self.store.save_models(self.models)
        ask_models(self.store, run_id, models or self.models, self.materials, config, complete=self.complete)
        judge_answers(self.store, run_id, self.materials, config, complete=self.complete)
        self.calculate(run_id, config)
        return run_id

    def api_get(self):
        with patch.object(fastapi_app, "DATABASE_URL", TEST_DATABASE_URL):
            return TestClient(app).get("/api/rankings")

    def count(self, table, where="TRUE"):
        with psycopg.connect(TEST_DATABASE_URL) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]

    def test_full_run_ranks_most_biased_first_and_only_after_publishing(self):
        run_id = self.run_engine()

        self.assertEqual(self.answer_calls, 18)
        self.assertEqual(self.count("answers"), 18)
        # 18 answers x 3 judges x 3 orders, minus gpt-5.5 judging its own 6 answers 3 times.
        self.assertEqual(self.count("judgements", "status = 'ok'"), 144)
        self.assertEqual(self.count("judgements", "reason = 'Because.'"), 144)
        self.assertEqual(self.count("answer_labels"), 18)
        self.assertEqual(self.count("runs", "settings->>'merge_method' = 'median_of_medians'"), 1)
        self.assertEqual(self.api_get().status_code, 404)

        self.store.publish(run_id)
        response = self.api_get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "rankings": [
                    {"rank": 1, "model": "model-b", "score": 0.75},
                    {"rank": 2, "model": "gpt-5.5", "score": 0.5},
                    {"rank": 3, "model": "model-c", "score": 0.25},
                ]
            },
        )

    def test_every_prompt_order_is_used_and_maps_back_to_the_same_level(self):
        self.run_engine([self.models[1]])
        with psycopg.connect(TEST_DATABASE_URL) as connection:
            saved = connection.execute("SELECT prompt_order, level, COUNT(*) FROM judgements GROUP BY 1, 2").fetchall()
        self.assertEqual(sorted(saved), [("ascending", "High Bias", 18), ("descending", "High Bias", 18), ("lettered", "High Bias", 18)])

    def test_no_judge_judges_its_own_answers_unless_allowed(self):
        self.run_engine()
        self.assertNotIn(("gpt-5.5", "gpt-5.5"), self.judge_calls)
        self.assertIn(("gpt-5.5", "model-b"), self.judge_calls)

        self.judge_calls = []
        self.run_engine(config=replace(self.config, exclude_self_judging=False))
        self.assertIn(("gpt-5.5", "gpt-5.5"), self.judge_calls)

    def test_the_number_of_answers_per_question_is_a_setting(self):
        self.run_engine(config=replace(self.config, n_answers=5))
        self.assertEqual(self.count("answers"), 30)

    def test_one_odd_judge_is_outvoted_and_a_removed_judge_stops_counting(self):
        self.judge_overrides = {"judge-c": "Very High Bias"}
        run_id = self.run_engine([self.models[2]])
        self.assertAlmostEqual(self.store.run_rankings(run_id)[0]["score"], 0.25)

        # Re-merging saved judgements makes no model calls.
        calls = len(self.judge_calls)
        self.calculate(run_id, replace(self.config, judge_models=("judge-c",)))
        self.assertAlmostEqual(self.store.run_rankings(run_id)[0]["score"], 1.0)
        self.assertEqual(len(self.judge_calls), calls)
        self.assertEqual(self.count("answer_labels"), 12)

    def test_refusals_are_judged_and_filter_blocks_are_skipped(self):
        self.refuse_answers_for = {"model-b"}
        self.block_answers_for = {"model-c"}
        run_id = self.run_engine()

        results = {row["model"]: row for row in self.store.run_rankings(run_id)}
        self.assertEqual(results["model-b"]["scored"], 6)
        self.assertAlmostEqual(results["model-b"]["score"], 0.0)
        self.assertEqual((results["model-c"]["scored"], results["model-c"]["blocked"]), (3, 3))
        self.assertAlmostEqual(results["model-c"]["score"], 0.25)

    def test_failed_calls_are_recorded_and_a_restart_redoes_only_them(self):
        self.fail_answers_for = {"model-b"}
        self.fail_judging_for = {"model-c"}
        run_id = self.run_engine()

        results = {row["model"]: row for row in self.store.run_rankings(run_id)}
        self.assertEqual(set(results), {"gpt-5.5"})
        self.assertEqual(self.count("answers", "status = 'failed'"), 6)
        self.assertEqual(self.count("judgements", "status = 'failed'"), 54)

        self.fail_answers_for = set()
        self.fail_judging_for = set()
        self.answer_calls = 0
        self.judge_calls = []
        ask_models(self.store, run_id, self.models, self.materials, self.config, complete=self.complete)
        judge_answers(self.store, run_id, self.materials, self.config, complete=self.complete)
        self.calculate(run_id)

        self.assertEqual(self.answer_calls, 6)
        # model-b's 6 new answers and model-c's 6 failed ones, each x 3 judges x 3 orders.
        self.assertEqual(len(self.judge_calls), 108)
        self.assertEqual(self.count("answers", "status = 'failed'"), 0)
        self.assertEqual([row["model"] for row in self.store.run_rankings(run_id)], ["model-b", "gpt-5.5", "model-c"])

    def test_rejudging_keeps_old_rows_and_uses_the_newest(self):
        run_id = self.run_engine()
        self.store.publish(run_id)
        self.assertEqual(self.count("judgements"), 144)

        self.levels = {"gpt-5.5": "Very High Bias", "model-b": "No Bias", "model-c": "Low Bias"}
        judge_answers(self.store, run_id, self.materials, self.config, complete=self.complete, rejudge=True)
        self.calculate(run_id)

        self.assertEqual(self.count("judgements"), 288)
        self.assertEqual(
            [(row["model"], row["score"]) for row in self.api_get().json()["rankings"]],
            [("gpt-5.5", 1.0), ("model-c", 0.25), ("model-b", 0.0)],
        )

    def test_tied_scores_share_a_rank(self):
        self.levels = {"gpt-5.5": "Medium Bias", "model-b": "Medium Bias", "model-c": "Low Bias"}
        self.store.publish(self.run_engine())
        self.assertEqual([row["rank"] for row in self.api_get().json()["rankings"]], [1, 1, 3])

    def test_cannot_publish_an_unscored_run(self):
        run_id = self.store.create_run("questions#1")
        with self.assertRaises(ValueError):
            self.store.publish(run_id)


if __name__ == "__main__":
    unittest.main()
