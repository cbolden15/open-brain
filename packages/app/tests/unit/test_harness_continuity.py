from open_brain.services.harness_continuity import capture_batches, indexed_memories
from open_brain.services.harness_memory import MemoryMessage


def test_all_later_statements_survive_message_and_selection_pages() -> None:
    messages = tuple(
        MemoryMessage("user", f"I prefer fixture{i} pencils.", i, True, f"entry-{i}")
        for i in range(300)
    )
    selected = indexed_memories(messages)
    batches = capture_batches(messages)
    assert len(selected) == 300
    assert [key for batch in batches for key in batch.selection] == [item.key for item in selected]
    assert max(len(batch.selection) for batch in batches) <= 32
    assert {m.source_id for batch in batches for m in batch.messages} == {
        m.source_id for m in messages
    }


def test_selection_index_does_not_shift_with_earlier_facts_or_recipe_batch() -> None:
    many = MemoryMessage(
        "user",
        "\n".join(f"I prefer fixture{i} pencils." for i in range(40)),
        5,
        True,
        "long-source",
    )
    recipe = (
        "Ingredients:\n- 1 cup lentils\n- 2 cups water\nDirections:\n"
        "1. Rinse lentils.\n2. Simmer 20 minutes."
    )
    messages = (
        many,
        MemoryMessage("user", "Please give me a complete soup recipe.", 6),
        MemoryMessage("assistant", recipe, 7, True, "recipe-source"),
    )
    batches = capture_batches(messages)
    assert len(batches) == 2
    assert (5, 39) in batches[-1].selection
    assert messages[1:] == batches[-1].messages[-2:]
    assert indexed_memories(messages)[-1].candidate.text == recipe
    assert indexed_memories((many,))[-1].key == (5, 39)
