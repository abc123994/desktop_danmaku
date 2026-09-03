from __future__ import annotations

import unittest

from overlay_position import initial_message_x


class InitialMessagePositionTests(unittest.TestCase):
    def test_message_starts_just_outside_right_edge(self) -> None:
        x = initial_message_x(0, 1919, 400)
        self.assertEqual(x, 1920.0)
        self.assertGreater(x, 1919)

    def test_entry_margin_is_outside_right_edge(self) -> None:
        self.assertEqual(initial_message_x(0, 1919, 2400, margin=30), 1949.0)

    def test_nonzero_desktop_origin_is_supported(self) -> None:
        self.assertEqual(initial_message_x(-1920, -1, 400), 0.0)


if __name__ == "__main__":
    unittest.main()
