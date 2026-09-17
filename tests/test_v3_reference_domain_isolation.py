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
            "metadata": {
                "stable_profile": {"structure": "山体环绕，雪中石阶，悬崖附近"},
                "stable_design": "空置山道，古式石栏",
            },
        })
        self.assertIn("空场景环境基准图", prompt)
        self.assertIn("环境结构是画面唯一可见内容", prompt)
        self.assertNotIn("stable_profile", prompt)
        self.assertNotIn("stable_design", prompt)
        for token in ("角色", "人物", "主角", "手持", "背负", "佩戴"):
            self.assertNotIn(token, prompt)

        template = get_reference_template("location")
        self.assertIn("empty environment plate", template.positive)
        self.assertIn("empty stairs", template.positive)
        self.assertNotIn("character", template.positive.lower())
        self.assertIn("tiny distant person", template.negative.lower())
        self.assertIn("character", template.negative.lower())

    def test_prop_positive_prompt_is_exactly_one_object(self):
        prompt = self.service._reference_prompt({
            "entity_type": "prop",
            "name": "古剑",
            "metadata": {
                "stable_profile": {"type": "武器", "design": "乌木剑身，暗银纹剑鞘"},
                "stable_design": "完整组装状态",
            },
        })
        self.assertIn("1:1单一道具产品设定图", prompt)
        self.assertIn("画面物理对象总数严格为1", prompt)
        self.assertIn("只表现一个古剑本体", prompt)
        self.assertNotIn("stable_profile", prompt)
        self.assertNotIn("stable_design", prompt)
        for token in ("角色", "人物", "主角", "手部", "手持", "背负", "佩戴"):
            self.assertNotIn(token, prompt)

        template = get_reference_template("prop")
        self.assertIn("exactly one physical prop total in frame", template.positive)
        self.assertIn("one complete assembled item", template.positive)
        self.assertNotIn("character", template.positive.lower())
        self.assertIn("multiple objects", template.negative.lower())
        self.assertIn("stable_profile", template.negative.lower())
        self.assertIn("hand", template.negative.lower())
        self.assertIn("character", template.negative.lower())

    def test_provider_prop_prompt_drops_character_context_schema_keys_and_global_prop_mixing(self):
        old_prompt = (
            "项目一致性参考资产：道具「玉佩」。\n"
            "最高优先级：严格保持下面已经确认的稳定视觉事实，不得用参考图版式重新设计角色。\n"
            "stable_profile.type：道具\n"
            "stable_profile.design：椭圆形，青玉材质\n"
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
                prop_rules={"bad": "古剑与玉佩成套出现"},
            ),
            contract_context="人物背负古剑并佩戴玉佩走在雪山石阶",
            reference=True,
        )
        positive = compiled.positive_prompt.lower()
        self.assertIn("single isolated product study of exactly one 玉佩", compiled.positive_prompt)
        self.assertIn("sole visual subject is the target prop itself", positive)
        self.assertIn("exactly one physical prop total in frame", positive)
        self.assertIn("椭圆形，青玉材质", compiled.positive_prompt)
        self.assertIn("caption-free", positive)
        self.assertNotIn("stable_profile", positive)
        self.assertNotIn("stable.profile", positive)
        self.assertNotIn("古剑与玉佩成套出现", compiled.positive_prompt)
        for token in (
            "character", "person", "facial", "hairstyle", "costume",
            "角色", "人物", "主角", "少年", "背负", "手持", "佩戴",
        ):
            self.assertNotIn(token, positive)
        self.assertIn("multiple objects", compiled.negative_prompt.lower())
        self.assertIn("stable_profile", compiled.negative_prompt.lower())
        self.assertIn("character", compiled.negative_prompt.lower())
        self.assertIn("hand", compiled.negative_prompt.lower())

    def test_provider_location_prompt_drops_character_context_schema_keys_and_prop_rules(self):
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
        self.assertNotIn("stable_profile", positive)
        self.assertNotIn("stable.profile", positive)
        for token in (
            "character", "person", "facial", "hairstyle", "costume",
            "角色", "人物", "主角", "少年", "背负", "手持", "佩戴",
        ):
            self.assertNotIn(token, positive)
        self.assertNotIn("古剑与玉佩", compiled.positive_prompt)
        self.assertIn("tiny distant person", compiled.negative_prompt.lower())
        self.assertIn("stable_profile", compiled.negative_prompt.lower())
        self.assertIn("character", compiled.negative_prompt.lower())


if __name__ == "__main__":
    unittest.main()
