from dataclasses import replace
import unittest
from unittest.mock import patch

from spacekeeper.app_matching import APP_TITLE_RULES, restore_name_key
from spacekeeper.planner import plan_restore
from tests.support import SESSION, observe, window


def document_title(title):
    return title.rsplit(' | page ', 1)[0]


class AppMatchingTests(unittest.TestCase):
    def test_registering_a_title_rule_is_sufficient_for_restore_planning(self):
        saved = observe(window(app='Synthetic Viewer', title='Report | page 1', minimized=True),
                        window(2, app='Synthetic Viewer', title='Appendix | page 1'),
                        session=replace(SESSION, login_session_id=200))
        actual = window(9, app='SYNTHETIC VIEWER', title='REPORT | page 2')
        current = observe(actual, window(10, app='Synthetic Viewer', title='Appendix | page 2'))
        self.assertTrue(all(d.target is None for d in plan_restore(saved, current).windows))
        with patch.dict(APP_TITLE_RULES, {'synthetic viewer': document_title}):
            plan = plan_restore(saved, current)
        self.assertFalse(plan.issues)
        self.assertEqual(plan.windows[0].target.identity, actual.identity)
        self.assertEqual(plan.windows[0].source_window_id, 1)
        self.assertTrue(plan.windows[0].target.minimized)

    def test_custom_rules_cannot_bypass_duplicate_detection(self):
        saved = observe(window(app='Synthetic Viewer', title='Report | page 1'),
                        session=replace(SESSION, login_session_id=200))
        for sources, current in (
            (saved, observe(window(9, app='Synthetic Viewer', title='Report | page 2'),
                            window(10, app='Synthetic Viewer', title='Report | page 3'))),
            (observe(saved.windows[0], window(2, app='Synthetic Viewer', title='Report | page 2'), session=saved.session),
             observe(window(9, app='Synthetic Viewer', title='Report | page 3'))),
        ):
            with self.subTest(sources=len(sources.windows), current=len(current.windows)):
                with patch.dict(APP_TITLE_RULES, {'synthetic viewer': document_title}):
                    plan = plan_restore(sources, current)
                self.assertTrue(all(d.target is None for d in plan.windows))
                self.assertTrue(all(i.reason.value == 'name_ambiguous' for i in plan.issues))

    def test_rules_do_not_merge_apps_or_affect_unregistered_titles(self):
        with patch.dict(APP_TITLE_RULES, {'synthetic viewer': document_title}):
            self.assertNotEqual(restore_name_key(window(app='Synthetic Viewer', title='Report | page 1')),
                                restore_name_key(window(app='Other Viewer', title='Report')))
            self.assertEqual(restore_name_key(window(app='Other Viewer', title='REPORT | page 1')),
                             ('other viewer', 'report | page 1'))

    def test_code_empty_suffix_does_not_collapse_unrelated_file_titles(self):
        self.assertNotEqual(restore_name_key(window(app='Code', title='one.py — ')),
                            restore_name_key(window(app='Code', title='two.py — ')))
