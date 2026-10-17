"""scroll_direction_conflict 与滚动方向配置回归测试。

- 方向冲突检测：auto 永不冲突；显式方向与皮肤 UpsideDown 声明
  不一致时才返回警告文本（只读判定，不影响渲染）。
- configure_runtime 校验：auto/down/up 合法，非法值抛 ValueError。
"""

import os
import sys
import unittest
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from osu_toolbox import config, scroll_direction_conflict  # noqa: E402


def _skin(upside_down):
    """构造最小皮肤对象（仅暴露 upsidedown 判定所需的字段）。"""
    return SimpleNamespace(upside_down=upside_down)


class ScrollDirectionConflictTests(unittest.TestCase):
    """scroll_direction_conflict 纯函数：方向 vs 皮肤声明。"""

    def test_auto_never_conflicts(self):
        # auto 跟随皮肤：无论上落/下落皮肤都不提示
        self.assertIsNone(scroll_direction_conflict("auto", _skin(True)))
        self.assertIsNone(scroll_direction_conflict("auto", _skin(False)))

    def test_down_matches_down_skin(self):
        self.assertIsNone(scroll_direction_conflict("down", _skin(False)))

    def test_down_conflicts_with_upside_skin(self):
        msg = scroll_direction_conflict("down", _skin(True))
        self.assertIsNotNone(msg)
        self.assertIn("上落式", msg)
        self.assertIn("下落式", msg)

    def test_up_matches_upside_skin(self):
        self.assertIsNone(scroll_direction_conflict("up", _skin(True)))

    def test_up_conflicts_with_down_skin(self):
        msg = scroll_direction_conflict("up", _skin(False))
        self.assertIsNotNone(msg)
        self.assertIn("下落式", msg)
        self.assertIn("上落式", msg)


class ScrollDirectionConfigTests(unittest.TestCase):
    """configure_runtime 对滚动方向的校验与默认值。"""

    def tearDown(self):
        config.configure_runtime(scroll_direction="auto")

    def test_default_is_auto(self):
        config.configure_runtime(scroll_direction="auto")
        self.assertEqual(config.SCROLL_DIRECTION, "auto")

    def test_valid_values(self):
        for value in ("auto", "down", "up"):
            config.configure_runtime(scroll_direction=value)
            self.assertEqual(config.SCROLL_DIRECTION, value)

    def test_invalid_value_raises(self):
        for value in ("sideways", "upward", "", "DOWNWARD"):
            with self.assertRaises(ValueError):
                config.configure_runtime(scroll_direction=value)


if __name__ == "__main__":
    unittest.main()
