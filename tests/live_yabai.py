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
