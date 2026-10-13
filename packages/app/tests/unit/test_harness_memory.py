from dataclasses import FrozenInstanceError

import pytest

from open_brain.services.harness_memory import MemoryMessage, select_memories


def test_early_user_statement_preserves_exact_source_and_locator() -> None:
    statement = "My preferred editor is  Vim."
    messages = [MemoryMessage("user", statement, 7)]
    messages.extend(MemoryMessage("user", "Thanks!", ordinal) for ordinal in range(8, 14))

    candidates = select_memories(messages)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.kind == "preference"
    assert candidate.text == "My preferred editor is Vim."
    assert candidate.source_excerpt == statement
    assert candidate.ordinal == 7
    assert candidate.role == "user"


def test_statement_kinds_and_later_changed_decision_remain_source_grounded() -> None:
    messages = [
        MemoryMessage("user", "I live in Sampletown. I prefer vegetarian meals.", 1),
        MemoryMessage("user", "We decided to use SQLite for the sample project.", 2),
        MemoryMessage("user", "We changed our decision to PostgreSQL for the sample project.", 8),
        MemoryMessage("user", "The sample project uses Python.", 9),
    ]

    candidates = select_memories(messages)

    assert [(item.kind, item.text) for item in candidates] == [
        ("fact", "I live in Sampletown."),
        ("preference", "I prefer vegetarian meals."),
        ("decision", "We decided to use SQLite for the sample project."),
        ("decision", "We changed our decision to PostgreSQL for the sample project."),
        ("fact", "The sample project uses Python."),
    ]
    assert [item.ordinal for item in candidates] == [1, 1, 2, 8, 9]
    assert all(
        item.source_excerpt in messages[item_index].text
        for item, item_index in zip(candidates, [0, 0, 1, 2, 3], strict=True)
    )


@pytest.mark.parametrize(
    "role,text,authored",
    [
        ("user", "Do I prefer vegetarian meals?", True),
        ("user", "I prefer vegetarian meals?", True),
        ("user", "Tell me which database the sample project uses.", True),
        ("user", "Please remember that the sample project uses Python.", True),
        ("user", "I think I prefer vegetarian meals.", True),
        ("user", "I might live in Sampletown next year.", True),
        ("user", "I am tired. Thanks! That worked.", True),
        ("assistant", "I think you prefer vegetarian meals.", True),
        ("assistant", "The sample project uses Python.", True),
        ("system", "I prefer vegetarian meals.", True),
        ("user", "I prefer vegetarian meals.", False),
        ("user", "> I prefer vegetarian meals.", True),
        ("user", "```text\nI prefer vegetarian meals.\n```", True),
    ],
)
def test_questions_commands_chatter_guesses_and_injected_context_are_not_memories(
    role: str,
    text: str,
    authored: bool,
) -> None:
    assert select_memories([MemoryMessage(role, text, 0, authored)]) == ()


RECIPE = """Simple chickpea salad

Ingredients:
- 1 cup cooked chickpeas
- 2 tablespoons lemon juice
- 1 tablespoon olive oil

Directions:
1. Rinse the chickpeas.
2. Mix all ingredients and serve chilled.
"""


def test_user_provided_recipe_is_retained_without_an_explicit_save_request() -> None:
    message = MemoryMessage("user", RECIPE, 8, source_id="native-recipe")
    selected = select_memories([message])
    assert len(selected) == 1
    assert selected[0].kind == "material"
    assert selected[0].text == RECIPE.strip()
    assert selected[0].role == "user"
    assert selected[0].source_excerpt == RECIPE
    assert selected[0].source_id == "native-recipe"


def test_requested_recipe_retains_complete_assistant_authored_material() -> None:
    messages = [
        MemoryMessage("user", "Give me a chickpea salad recipe.", 20),
        MemoryMessage("system", "I prefer short answers.", 21, authored=False),
        MemoryMessage("assistant", RECIPE, 22),
    ]

    candidates = select_memories(messages)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.kind == "material"
    assert candidate.role == "assistant"
    assert candidate.ordinal == 22
    assert candidate.text == RECIPE.strip()
    assert candidate.source_excerpt == RECIPE


@pytest.mark.parametrize(
    "user_request,response,authored",
    [
        ("Thanks!", RECIPE, True),
        ("Give me a chickpea salad recipe.", RECIPE, False),
        ("Give me a chickpea salad recipe.", "Try chickpeas and lemon.", True),
        ("Give me a chickpea salad recipe.", "Ingredients:\n- chickpeas\nDirections:\nMix.", True),
        ("Do you like this recipe?", RECIPE, True),
    ],
)
def test_unrequested_or_incomplete_recipe_does_not_become_material(
    user_request: str,
    response: str,
    authored: bool,
) -> None:
    assert (
        select_memories(
            [
                MemoryMessage("user", user_request, 0, authored),
                MemoryMessage("assistant", response, 1),
            ]
        )
        == ()
    )


def test_new_user_message_expires_recipe_request() -> None:
    assert (
        select_memories(
            [
                MemoryMessage("user", "Give me a chickpea salad recipe.", 0),
                MemoryMessage("assistant", "Which ingredients do you have?", 1),
                MemoryMessage("user", "Never mind, explain SQLite.", 2),
                MemoryMessage("assistant", RECIPE, 3),
            ]
        )
        == ()
    )


def test_dataclasses_are_immutable() -> None:
    message = MemoryMessage("user", "I live in Sampletown.", 0)
    candidate = select_memories([message])[0]
    with pytest.raises(FrozenInstanceError):
        message.text = "edited"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        candidate.text = "edited"  # type: ignore[misc]


def test_memory_count_and_utf8_output_are_bounded_without_truncating_sources() -> None:
    messages = [MemoryMessage("user", f"I prefer sample option {i}.", i) for i in range(100)]
    assert len(select_memories(messages)) == 32

    statement = "I prefer " + "é" * 2500 + "."
    candidates = select_memories([MemoryMessage("user", statement, i) for i in range(10)])
    assert len(candidates) == 3
    assert sum(len(candidate.text.encode("utf-8")) for candidate in candidates) <= 16 * 1024
    assert all(candidate.source_excerpt == statement for candidate in candidates)


def test_oversized_statement_is_omitted_without_partial_memory() -> None:
    too_large = "I prefer " + "é" * 5000 + "."
    assert select_memories([MemoryMessage("user", too_large, 0)]) == ()


def test_input_message_bound_is_explicit() -> None:
    messages = [MemoryMessage("user", "Thanks!", i) for i in range(256)]
    messages.append(MemoryMessage("user", "I prefer sample option 257.", 256))
    assert select_memories(messages) == ()


def test_input_byte_bound_stops_scanning_and_oversized_request_cannot_authorize_material() -> None:
    messages = [MemoryMessage("user", "x" * (16 * 1024), i) for i in range(8)]
    messages.append(MemoryMessage("user", "I prefer sample option 9.", 8))
    assert select_memories(messages) == ()

    assert (
        select_memories(
            [
                MemoryMessage("user", "Give me a recipe. " + "x" * (16 * 1024), 0),
                MemoryMessage("assistant", RECIPE, 1),
            ]
        )
        == ()
    )


def test_mixed_request_keeps_only_independently_stated_fact() -> None:
    text = "The sample project uses Python 3.14. Can you explain type hints?"
    candidates = select_memories([MemoryMessage("user", text, 9)])
    assert len(candidates) == 1
    assert candidates[0].text == "The sample project uses Python 3.14."
    assert candidates[0].source_excerpt == "The sample project uses Python 3.14."


@pytest.mark.parametrize(
    "statement",
    [
        "We would have decided to use SQLite if this were a demo.",
        "My home is probably Sampletown.",
        "Our sample project should use Python.",
    ],
)
def test_hypothetical_statements_do_not_become_confirmed_knowledge(statement: str) -> None:
    assert select_memories([MemoryMessage("user", statement, 0)]) == ()


def test_conditional_preference_retains_the_condition_without_generalizing() -> None:
    statement = "I prefer dark mode if we end up working at night."
    candidates = select_memories([MemoryMessage("user", statement, 0)])
    assert len(candidates) == 1
    assert candidates[0].text == statement
    assert candidates[0].source_excerpt == statement


@pytest.mark.parametrize(
    "statement",
    [
        "I prefer.",
        "We decided.",
        "We decided to.",
        "The sample project uses.",
        "My preference is.",
        "My preferred editor.",
    ],
)
def test_incomplete_statements_are_not_useful_memories(statement: str) -> None:
    assert select_memories([MemoryMessage("user", statement, 0)]) == ()


def test_classification_normalizes_whitespace_but_keeps_exact_excerpt() -> None:
    statement = "I  prefer\t vegetarian meals."
    candidates = select_memories([MemoryMessage("user", statement, 0)])
    assert len(candidates) == 1
    assert candidates[0].text == "I prefer vegetarian meals."
    assert candidates[0].source_excerpt == statement


def test_oversized_exact_source_cannot_fit_only_by_normalizing_whitespace() -> None:
    statement = "I prefer " + " " * 9000 + "Vim."
    assert select_memories([MemoryMessage("user", statement, 0)]) == ()
