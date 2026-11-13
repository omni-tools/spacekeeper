import unittest

from spacekeeper.restore import RestoreIssue, RestoreReport, WindowOutcome


def report(*windows, issues=(), complete=False):
    return RestoreReport(complete, 'backup', 'report', tuple(windows), tuple(issues))


class RestoreReportTests(unittest.TestCase):
    def test_failed_target_and_multiple_issues_count_as_one_window(self):
        result = report(WindowOutcome(9, 'failed', 'final_state_mismatch', source_window_id=1), issues=(
            RestoreIssue('planning', 'unsupported', window_id=9, source_window_id=1),
            RestoreIssue('visibility', 'timeout', window_id=9),
            RestoreIssue('verification', 'window_target_mismatch', window_id=9),
            RestoreIssue('verification', 'focused_window_mismatch', window_id=9),
        ))
        self.assertEqual(result.missed, 1)
        self.assertFalse(result.hard_failure)

    def test_persistent_ax_lookup_and_dependent_ordering_are_partial_failures(self):
        result = report(WindowOutcome(1, 'skipped', 'window_missing'),
                        WindowOutcome(2, 'restored', 'restore_saved'), issues=(
            RestoreIssue('ordering', 'fullscreen_order_blocked', window_id=1),
            RestoreIssue('verification', 'window_target_mismatch', window_id=1),
            RestoreIssue('verification', 'fullscreen_order_target_changed', window_id=1),
        ))
        self.assertEqual(result.missed, 1)
        self.assertFalse(result.hard_failure)

    def test_loss_of_session_is_fatal_even_when_attached_to_a_window(self):
        for result in (report(WindowOutcome(1, 'failed', 'session_changed')),
                       report(issues=(RestoreIssue('visibility', 'session_changed', window_id=1),))):
            with self.subTest(result=result):
                self.assertTrue(result.hard_failure)

    def test_unrelated_window_changes_remain_hard_failures(self):
        for reason in ('preserved_window_changed', 'extra_ordinary_changed', 'new_fullscreen_window'):
            with self.subTest(reason=reason):
                self.assertTrue(report(issues=(RestoreIssue('verification', reason, window_id=9),)).hard_failure)

    def test_unknown_verification_failure_is_not_silently_downgraded(self):
        self.assertTrue(report(issues=(RestoreIssue('verification', 'unknown_failure', window_id=1),)).hard_failure)

    def test_source_ids_and_live_ids_do_not_collide(self):
        result = report(WindowOutcome(1, 'failed', 'timeout', source_window_id=2), issues=(
            RestoreIssue('planning', 'name_no_match', source_window_id=1),
            RestoreIssue('verification', 'window_target_mismatch', window_id=1),
        ))
        self.assertEqual(result.missed, 2)

    def test_issue_binding_deduplicates_an_unsupported_source_and_changed_live_window(self):
        result = report(WindowOutcome(9, 'excluded', 'fallback_unmatched'), issues=(
            RestoreIssue('planning', 'metadata_unavailable', window_id=9, source_window_id=5),
            RestoreIssue('verification', 'fallback_unmatched_changed', window_id=9),
        ))
        self.assertEqual(result.missed, 1)

    def test_healthy_exclusions_and_monitor_fallback_do_not_count(self):
        result = report(WindowOutcome(1, 'excluded', 'protected_window'),
                        WindowOutcome(2, 'excluded', 'preserved_extra'),
                        WindowOutcome(3, 'excluded', 'fallback_unmatched'),
                        WindowOutcome(4, 'unchanged', 'already_matches', monitor_fallback=True), complete=True)
        self.assertEqual(result.missed, 0)
        self.assertFalse(result.hard_failure)

    def test_same_session_closed_window_and_focus_issue_are_deduplicated(self):
        result = report(WindowOutcome(1, 'skipped', 'closed'), issues=(
            RestoreIssue('planning', 'focus_unavailable', window_id=1),
        ))
        self.assertEqual(result.missed, 1)
        self.assertFalse(result.hard_failure)

    def test_global_verification_failure_is_not_invented_as_a_window(self):
        result = report(issues=(RestoreIssue('verification', 'timeout'),))
        self.assertEqual(result.missed, 0)
        self.assertTrue(result.hard_failure)

    def test_missing_sources_are_partial_results_and_rejected_sessions_are_hard_failures(self):
        partial = report(issues=(RestoreIssue('planning', 'name_no_match', source_window_id=7),))
        self.assertEqual(partial.missed, 1)
        self.assertFalse(partial.hard_failure)
        rejected = report(issues=(RestoreIssue('planning', 'session_mismatch'),))
        self.assertTrue(rejected.hard_failure)
