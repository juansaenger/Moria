from homebot.discord_bot import split_message


def test_short_message_untouched():
    assert split_message("hello") == ["hello"]


def test_splits_on_newlines_under_limit():
    text = "\n".join(f"line {i}" * 20 for i in range(40))
    chunks = split_message(text, limit=500)
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "".join(chunk.replace("\n", "") for chunk in chunks) == text.replace("\n", "")


def test_hard_split_without_whitespace():
    chunks = split_message("x" * 4500)
    assert [len(c) for c in chunks] == [2000, 2000, 500]
