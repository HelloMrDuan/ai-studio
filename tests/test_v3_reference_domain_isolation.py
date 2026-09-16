from __future__ import annotations

import unittest

from app.services.reference_templates import get_reference_template
from app.v3.reference_assets import ReferenceAssetBootstrap


class _Legacy:
    director = object()


class ReferenceDomainIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ReferenceAssetBootstrap(_Legacy(), submit_candidate=lambda *_args, **_kwargs: None)

    def test_character_prompt_contract_is_unchanged(self):
        prompt = self.service._reference_prompt({
            "entity_type": "character",
            "name": "沈川",
            "metadata": {"appearance": "17岁，黑发，深蓝长袍"},
        })
        self.assertIn("不得用参考图版式重新设计角色", prompt)
        self.assertIn("4:3横向角色三视图设定图", prompt)
        self.assertIn("严格90度侧面全身", prompt)

    def test_location_positive_prompt_is_environment_only(self):
        prompt = self.service._reference_prompt({
            "entity_type": "location",
            "name": "青云山",
            "metadata": {"structure": "山体环绕，雪中石阶，悬崖附近"},
        })
        self.assertIn("空场景环境基准图", prompt)
        self.assertIn("环境结构本身为唯一叙事中心", prompt)
        for token in ("角色", "人物", "主角", "手持", "背负", "佩戴"):
            self.assertNotIn(token, prompt)

        template = get_reference_template("location")
        self.assertIn("empty environment plate", template.positive)
        self.assertNotIn("character", template.positive.lower())
        self.assertIn("character", template.negative.lower())

    def test_prop_positive_prompt_is_object_only(self):
        prompt = self.service._reference_prompt({
            "entity_type": "prop",
            "name": "古剑",
            "metadata": {"type": "武器", "design": "乌木剑身，暗银纹剑鞘"},
        })
        self.assertIn("单一道具产品设定图", prompt)
        self.assertIn("唯一主体就是该道具本体", prompt)
        for token in ("角色", "人物", "主角", "手部", "手持", "背负", "佩戴"):
            self.assertNotIn(token, prompt)

        template = get_reference_template("prop")
        self.assertIn("sole visual subject is the prop itself", template.positive)
        self.assertNotIn("character", template.positive.lower())
        self.assertIn("hand", template.negative.lower())
        self.assertIn("character", template.negative.lower())


if __name__ == "__main__":
    unittest.main()
