from dataclasses import replace

import pytest
from open_brain_engine.engine.harness_memory import HarnessCitation, HarnessMemory

from open_brain.services.harness_retrieval import decision_state, plan_task_query


def memory(text: str, *, kind: str = "decision", ordinal: int = 1) -> HarnessMemory:
    return HarnessMemory(
        kind,
        text,
        text,
        HarnessCitation("codex", "codex", "project-test", "session-test", ordinal, "user", None),
    )


@pytest.mark.parametrize(
    "task,statement,kind",
    [
        ("What is my hometown?", "I live in Exampleville.", "fact"),
        ("Draft a note about my employer", "I work for Example Company.", "fact"),
        ("Configure my editing environment", "My preferred editor is Helix.", "preference"),
        ("Which storage choice did we make?", "We decided to use DuckDB.", "decision"),
        ("Prepare lunch for tomorrow", "I prefer vegetarian meals.", "preference"),
        ("Pick food to cook", "I am allergic to peanuts.", "fact"),
    ],
)
def test_task_domains_match_stated_knowledge_without_specific_fixture_values(
    task: str,
    statement: str,
    kind: str,
) -> None:
    plan = plan_task_query(task)
    assert plan.relevance(memory(statement, kind=kind)) > 0
    assert plan.relevance(memory("My preferred notebook is violet.", kind="preference")) == 0


def test_meal_intent_retrieves_unrelated_recipe_titles_without_admitting_generic_preferences() -> (
    None
):
    plan = plan_task_query("Help plan meals for next week")
    recipe = memory(
        "Roasted carrots\nIngredients\n- 4 carrots\n- 2 tablespoons oil\nDirections\n"
        "1. Cut carrots.\n2. Roast for 20 minutes.",
        kind="material",
    )
    assert plan.relevance(recipe) > 0
    assert plan.relevance(memory("I prefer green curtains.", kind="preference")) == 0


def test_stopwords_alone_do_not_retrieve_arbitrary_memories() -> None:
    assert plan_task_query("Please help me with this").search_text == ""
    plan = plan_task_query("Help with orbital mechanics")
    assert plan.relevance(memory("We decided to use SQLite for this project.")) == 0


@pytest.mark.parametrize("query", ["", " ", "x" * 501])
def test_invalid_query_is_bounded(query: str) -> None:
    with pytest.raises(ValueError, match="invalid_query"):
        plan_task_query(query)


def test_cross_session_change_requires_source_timestamps_or_withholds_uncertain_currentness() -> (
    None
):
    original = memory("We decided to use SQLite for the archive project.")
    change = memory("We changed our decision to DuckDB for the archive project.")
    change = replace(change, citation=replace(change.citation, session_id="later-session"))
    assert decision_state(original, [change]) == "currentness_unknown"
    original = replace(
        original, citation=replace(original.citation, timestamp="2026-01-01T00:00:00Z")
    )
    change = replace(change, citation=replace(change.citation, timestamp="2026-01-02T00:00:00Z"))
    assert decision_state(original, [change]) == "superseded"
    assert decision_state(change, [original]) == "current"


def test_earlier_change_and_other_projects_do_not_supersede_current_decision() -> None:
    current = memory("We decided to use SQLite for the archive project.", ordinal=9)
    earlier = memory("We changed our decision to DuckDB for the archive project.")
    other = replace(earlier, citation=replace(earlier.citation, project_id="project-other"))
    assert decision_state(current, [earlier, other]) == "current"


def test_unnamed_explicit_same_session_change_conservatively_withholds_prior_decisions() -> None:
    original = memory("We decided to use SQLite.")
    change = memory("We changed our decision to DuckDB.", ordinal=9)
    assert decision_state(original, [change]) == "superseded"
    guess = memory("We might change our decision to DuckDB.", ordinal=10)
    assert decision_state(original, [guess]) == "current"
