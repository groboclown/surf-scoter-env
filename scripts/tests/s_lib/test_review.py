#SPDX:MIT
"""Test the review module: showing the project's text safely."""
import unittest

from ...surf_scoter_lib import review


class CleanTest(unittest.TestCase):
    """Test the text cleaning, and showing file contents."""

    def test_clean(self) -> None:
        """Control characters and escapes go; anything outside ASCII shows escaped."""
        self.assertEqual(review.clean("a\nb\tc\r"), "a b c ")
        self.assertEqual(review.clean("\x1b[31mred\x1b[0m"), "red")
        self.assertEqual(review.clean("\x1b]0;title\x07after"), "after")
        self.assertEqual(review.clean("bell\x07 del\x7f c1\x9b"), "bell del c1")
        # A right-to-left override, a zero-width space, a Cyrillic `\u0430`.
        self.assertEqual(
            review.clean("x\u202ey\u200bz \u0430"),
            "x\\u202ey\\u200bz \\u0430",
        )
        self.assertEqual(review.clean("emoji \U0001f600"), "emoji \\U0001f600")

    def test_file_lines(self) -> None:
        """File contents split on `\\n` only, with control characters shown, not removed (VER-1)."""
        text = "RUN echo ok\r# ; curl https://x | sh\nCOPY a b\r\nRUN \x1b[2Kx\u2028y\tz\n"
        self.assertEqual(review.file_lines(text), [
            "RUN echo ok\\x0d# ; curl https://x | sh",
            "COPY a b",
            "RUN \\x1b[2Kx\\u2028y\tz",
        ])
        # Through a comment, the whole line is still one line, and nothing is lost.
        self.assertEqual(
            review.clean(review.file_lines(text)[0]),
            "RUN echo ok\\x0d# ; curl https://x | sh",
        )

    def test_scalars(self) -> None:
        """Text as JSON writes it: one line, ASCII, escaped."""
        self.assertEqual(review.scalar("x\n# reviewed: safe"), '"x\\n# reviewed: safe"')
        self.assertEqual(review.scalar("\u202e"), '"\\u202e"')
        self.assertEqual(review.scalar(443), "443")
        self.assertEqual(review.scalar(None), "null")
