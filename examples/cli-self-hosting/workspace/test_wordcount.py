import unittest

from wordcount import count_words


class CountWordsTest(unittest.TestCase):
    def test_single_spaces(self) -> None:
        self.assertEqual(count_words('one two three'), 3)

    def test_runs_of_whitespace(self) -> None:
        self.assertEqual(count_words('one  two\n three'), 3)

    def test_empty_text_has_no_words(self) -> None:
        self.assertEqual(count_words(''), 0)


if __name__ == '__main__':
    unittest.main()
