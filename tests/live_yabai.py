"""Opt-in read-only observation: python -m unittest tests.live_yabai -v.

Queries displays/Spaces/windows in memory only. No titles or identity values are
printed or written, and no window commands are issued.
"""

import sys
import unittest

from spacekeeper.snapshot import Snapshot
from spacekeeper.yabai import Yabai


@unittest.skipUnless(sys.platform == "darwin", "Requires macOS and a running yabai service")
class LiveYabaiTests(unittest.TestCase):
    def test_read_only_observation(self):
        observation = Yabai().observe()
        self.assertTrue(len(observation.displays) > 0, "No displays were observed.")
        self.assertTrue(len(observation.spaces) > 0, "No Spaces were observed.")
        self.assertTrue(Snapshot.from_json(observation.to_json()) == observation,
                        "Observation did not survive serialization.")

    def test_read_only_target_observation(self):
        desktop = Yabai()
        snapshot = desktop.observe()
        target = next((w for w in snapshot.windows if w.identity is not None and w.root_window and
                       (w.role, w.subrole) == ('AXWindow', 'AXStandardWindow') and
                       not (w.hidden or w.minimized or w.sticky)), None)
        if target is None:
            self.skipTest('No supported window is available for read-only target queries.')
        observed = desktop.observe_window(target.identity, snapshot.session)
        self.assertEqual(observed.window.identity, target.identity, 'Target ownership changed during observation.')
        self.assertIn(observed.window.display_uuid, {d.uuid for d in observed.displays},
                      'Target monitor was not resolved.')

    def test_read_only_preset_plan_uses_current_owners(self):
        from glob import escape
        import yaml
        from spacekeeper.planner import unsupported_reason
        from spacekeeper.presets import Preset
        snapshot = Yabai().observe()
        target = next((w for w in snapshot.windows if unsupported_reason(w) is None and
                       sum((x.app.casefold(), x.title.casefold()) == (w.app.casefold(), w.title.casefold())
                           for x in snapshot.windows) == 1), None)
        if target is None:
            self.skipTest('No uniquely selectable supported window is available.')
        preset = Preset.from_yaml(yaml.safe_dump({'windows': [{'app': escape(target.app), 'title': escape(target.title),
                                                              'device': target.display_uuid,
                                                              'fullscreen': target.fullscreen}]}))
        plan = preset.plan(snapshot)
        self.assertTrue(plan.accepted and all(d.target is None or d.target.identity == target.identity
                                             for d in plan.windows), 'Preset selected an unexpected identity.')
