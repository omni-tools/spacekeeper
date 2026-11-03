import unittest

from spacekeeper.matching import WindowRule, window_rules
from tests.support import window


class MatchingTests(unittest.TestCase):
    def test_app_and_title_globs_are_case_insensitive_and_both_required(self):
        rule = WindowRule('synthetic*', '*DOCUMENT')
        self.assertTrue(rule.matches(window()))
        self.assertFalse(WindowRule('other*').matches(window()))
        self.assertFalse(WindowRule('*', '*meeting*').matches(window()))

    def test_malformed_rules_are_rejected(self):
        for data in ({}, [None], [{'title': '*'}], [{'app': ''}], [{'app': '*', 'extra': 1}],
                     [{'app': True}], [{'app': '*', 'title': None}], [{'app': 'bad\0pattern'}]):
            with self.subTest(data=data), self.assertRaises(ValueError):
                window_rules(data)
