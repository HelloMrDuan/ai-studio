from __future__ import annotations

import unittest

from app.services.prompt_compiler import PromptCompiler
from app.services.reference_templates import get_reference_template
from app.services.visual_direction import VisualDirection
from app.v3.reference_assets import ReferenceAssetBootstrap


class _Legacy:
    director = object()


class ReferenceDomainIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ReferenceAssetBootstrap(_Legacy(), submit_candidate=lambda *_args, **_kwargs: None)
        self.compiler = PromptCompiler()

    def test_character_prompt_contract_is_unchanged(self):
        prompt = self.service._reference_prompt({
            "entity_type": "character",
            "name": "沈川",
            "metadata": {"appearance": "17岁，黑发，深蓝长袍"},
        })
        self.assertIn("不得用参考图版式重新设计角色", prompt)
        self.assertIn("4:3横向角色三视图设定图", prompt)
        self.assertIn("严格90度侧面全身", prompt)

        compiled = self.compiler.compile(
            asset_kind="character",
            asset_description=prompt,
            visual_direction=VisualDirection(
                world_style="东方仙侠",
                culture="中国",
                era="古代",
                art_style="电影写实",
                character_rules={"identity": "角色五官与发型保持统一"},
            ),
            reference=True,
        )
        self.assertIn("East Asian facial identity", compiled.positive_prompt)
        self.assertIn("ancient Chinese costume language", compiled.positive_prompt)
        self.assertIn("角色五官与发型保持统一", compiled.positive_prompt)

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

    def test_provider_prop_prompt_drops_character_context_and_legacy_prompt_leakage(self):
        old_prompt = (
            "项目一致性参考资产：道具「古剑」。\n"
            "最高优先级：严格保持下面已经确认的稳定视觉事实，不得用参考图版式重新设计角色。\n"
            "stable_profile.type：武器\n"
            "stable_profile.design：乌木剑身，暗银纹剑鞘\n"
            "不出现人物、手持、背负或佩戴。"
        )
        compiled = self.compiler.compile(
            asset_kind="prop",
            asset_description=old_prompt,
            visual_direction=VisualDirection(
                world_style="东方仙侠",
                culture="中国",
                era="古代",
                art_style="电影写实",
                character_rules={"bad": "East Asian facial identity, traditional hairstyle"},
                environment_rules={"bad": "雪山中站着少年主角"},
                prop_rules={"craft": "乌木、暗银、旧金属传统工艺"},
            ),
            contract_context="人物背负古剑走在雪山石阶",
            reference=True,
        )
        positive = compiled.positive_prompt.lower()
        self.assertIn("sole visual subject is the prop itself", positive)
        self.assertIn("乌木、暗银、旧金属传统工艺", compiled.positive_prompt)
        self.assertIn("caption-free", positive)
        for token in (
            "character", "person", "facial", "hairstyle", "costume",
            "角色", "人物", "主角", "少年", "背负", "手持", "佩戴",
        ):
            self.assertNotIn(token, positive)
        self.assertIn("character", compiled.negative_prompt.lower())
        self.assertIn("hand", compiled.negative_prompt.lower())

    def test_provider_location_prompt_drops_character_context_and_character_rules(self):
        old_prompt = (
            "项目一致性参考资产：场景「青云山」。\n"
            "最高优先级：严格保持下面已经确认的稳定视觉事实，不得用参考图版式重新设计角色。\n"
            "stable_profile.structure：山体环绕，雪中石阶，悬崖附近\n"
            "不加入主角或剧情动作。"
        )
        compiled = self.compiler.compile(
            asset_kind="location",
            asset_description=old_prompt,
            visual_direction=VisualDirection(
                world_style="东方仙侠",
                culture="中国",
                era="古代",
                art_style="电影写实",
                character_rules={"bad": "少年黑发，古代长袍，traditional hairstyle"},
                environment_rules={"terrain": "山体环绕，雪中石阶，悬崖与古式石栏"},
                prop_rules={"bad": "古剑与玉佩"},
            ),
            contract_context="主角背负古剑走上石阶",
            reference=True,
        )
        positive = compiled.positive_prompt.lower()
        self.assertIn("empty environment plate", positive)
        self.assertIn("山体环绕，雪中石阶，悬崖与古式石栏", compiled.positive_prompt)
        self.assertIn("caption-free", positive)
        for token in (
            "character", "person", "facial", "hairstyle", "costume",
            "角色", "人物", "主角", "少年", "背负", "手持", "佩戴",
        ):
            self.assertNotIn(token, positive)
        self.assertNotIn("古剑与玉佩", compiled.positive_prompt)
        self.assertIn("character", compiled.negative_prompt.lower())


if __name__ == "__main__":
    unittest.main()
