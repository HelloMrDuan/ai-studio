from __future__ import annotations

import unittest

from app.v3.character_visual_continuity import _append_clause, _continuity_clause


class CharacterVisualContinuityTests(unittest.TestCase):
    def entity(self) -> dict:
        return {
            "entity_id": "char-1",
            "entity_type": "character",
            "name": "苏瑶",
            "metadata": {
                "typed_character_contract": {
                    "服装": "浅青色古代交领长裙，白色内衬",
                    "鞋履": "浅色绣鞋",
                    "固定身份锚点": ["黑色长发", "白玉发簪"],
                }
            },
        }

    def test_clause_makes_typed_wardrobe_authoritative_over_reference_pixels(self) -> None:
        clause = _continuity_clause(self.entity())
        self.assertIn("服装=浅青色古代交领长裙，白色内衬", clause)
        self.assertIn("鞋履=浅色绣鞋", clause)
        self.assertIn("参考图中的偶然服装、配色或鞋履偏差", clause)
        self.assertIn("脸部参考只负责锁定身份", clause)
        self.assertIn("不得擅自改成任何未确认颜色", clause)

    def test_append_is_idempotent_and_does_not_touch_non_character(self) -> None:
        once = _append_clause("基础生成要求", self.entity())
        twice = _append_clause(once, self.entity())
        self.assertEqual(once, twice)
        location = {"entity_type": "location", "metadata": {}}
        self.assertEqual(_append_clause("场景要求", location), "场景要求")


if __name__ == "__main__":
    unittest.main()
