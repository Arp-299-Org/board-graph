"""Regression tests for saved-board semantic comparison."""
import contextlib
import io
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import kicad_compare_board_semantics as comparison


BEFORE = '''(kicad_pcb
  (footprint "Test" (property "Reference" "R1") (at 4 5) (uuid "aaa"))
  (segment (start 1 23) (end 8 9) (uuid "bbb"))
  (gr_line (start 2 34) (end 0 0)))'''


def run_check(before, after):
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        board = root / 'board.kicad_pcb'
        board.write_text(after, encoding='utf-8')
        with (mock.patch.object(comparison, '__file__', str(root / 'scripts' / 'compare.py')),
              mock.patch.object(comparison, 'git_baseline', return_value=before),
              mock.patch.object(sys, 'argv', ['compare', '--board', str(board), '--allow-footprint-ref', 'R1']),
              contextlib.redirect_stdout(io.StringIO())):
            comparison.main()


class SemanticComparisonTests(unittest.TestCase):
    def test_keyed_coordinate_atom_boundary_is_not_erased(self):
        with self.assertRaisesRegex(comparison.CompareError, 'Unapproved UUID-bearing'):
            run_check(BEFORE, BEFORE.replace('(start 1 23)', '(start 12 3)'))

    def test_unkeyed_coordinate_atom_boundary_is_not_erased(self):
        with self.assertRaisesRegex(comparison.CompareError, 'Unapproved unkeyed'):
            run_check(BEFORE, BEFORE.replace('(start 2 34)', '(start 23 4)'))

    def test_formatting_and_item_order_do_not_change_the_comparison(self):
        after = '''( kicad_pcb
          ( gr_line ( start 2\t34 ) (end 0 0))
          ( segment\n(start 1 23)( end 8 9 )( uuid "bbb" ))
          ( footprint "Test" ( property "Reference" "R1" ) ( at 4 5 ) ( uuid "aaa" )))'''
        run_check(BEFORE, after)

    def test_quoted_values_and_escaped_quotes_are_preserved(self):
        original = r'(property "Value" "left  1 23 \"quoted\" right")'
        self.assertEqual(comparison.compact(original), comparison.compact('  ' + original + '  '))
        self.assertNotEqual(comparison.compact(original), comparison.compact(original.replace('1 23', '12 3')))
        self.assertNotEqual(comparison.compact(original), comparison.compact(original.replace('left  ', 'left ')))

    def test_non_ascii_space_inside_an_atom_is_not_discarded(self):
        self.assertNotEqual(comparison.compact('(text A\u00a0B)'), comparison.compact('(text A B)'))


if __name__ == '__main__':
    unittest.main()
