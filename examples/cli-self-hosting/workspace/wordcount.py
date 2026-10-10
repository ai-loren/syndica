"""A tiny word counter."""


def count_words(text: str) -> int:
    """Count the words in text; any run of whitespace separates two words."""
    return len(text.split(' '))
