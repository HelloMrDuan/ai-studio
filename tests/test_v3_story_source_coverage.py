from __future__ import annotations

import unittest

from app.v3.story_source_coverage import (
    extract_story_table_names,
    infer_source_character_candidates,
    source_coverage_issues,
)


_NATURAL_STORY = """
项目名：《雪夜归城》

大雪初停，北方古城的城门已经落锁。

沈璃独自站在城外石桥上。寒风卷起她斗篷的一角，露出里面深红色的交领长裙。她抬手压住被风吹乱的鬓发，银色梅花簪在暮色里闪了一下。

桥下的河已经结冰，远处城墙上的积雪被晚霞染成暗红。

她提着一盏六角青铜灯笼，灯笼上的梅花纹被烛火照得忽明忽暗。

马蹄声从雪后的官道上传来。

陆沉勒马停在桥头。他从马背翻身落地，深青色长袍下摆沾着一路风雪，乌木剑鞘斜背在身后。

沈璃看了他一眼。

“你怎么回来了？”

陆沉没有回答，只抬头看向紧闭的城门。

下一刻，城墙上的烽火台突然亮起一道红光。

紧接着，城门后传来一声沉重的撞击。

沈璃手中的灯笼微微一晃。

陆沉已经按住了剑柄。

“别站在这里。”

他拔剑出鞘，踩着积雪朝城门走去。

沈璃提灯跟了上去。
"""

_STAGE01_MISSING_SHENLI = """
# 故事生产圣经
## 故事定位
雪夜古城危机短篇。
## 不可篡改事实
沈璃与陆沉在古城门外会合并向城门前进。
## 世界观与时间线
古代北方城镇；大雪初停后的傍晚连续发生。
## 剧情节点
N01 石桥会合；N02 烽火亮起；N03 城门异响。
## 角色实体表
陆沉。
## 地点实体表
城外石桥；古城城门。
## 道具实体表
乌木剑鞘；六角青铜灯笼。
## 对白与旁白事实
对白：保留“你怎么回来了？”与“别站在这里。”；旁白：保留雪夜、烽火和城门异响。
## 连续性事件
灯笼由沈璃持有；长剑属于陆沉；两人从石桥走向城门。
## 创作计划
先建立雪夜危机，再以烽火与撞击升级悬念。
"""

_STAGE01_COMPLETE = _STAGE01_MISSING_SHENLI.replace("## 角色实体表\n陆沉。", "## 角色实体表\n沈璃；陆沉。")

_STAGE02_ONLY_SHENLI = """
## 角色资产：沈璃
- 性别呈现：女性
- 年龄感：青年
- 脸部结构与五官：待设计
- 发型：低髻
- 发色：黑色
- 肤色：偏白
- 体型：纤细
- 身高感：中等
- 服装：深红交领长裙
- 鞋履：布靴
- 固定身份锚点：低髻与银色梅花簪
- 允许变化项：表情
- 形象版本：default
- change_reason：基础造型
- 参考图生成要求：身份参考
- 原文证据：原文明确
- 设计补全来源：未指定项待角色设计
```appearance-versions-json
{"versions":[{"appearance_id":"default","name":"默认造型","stable_design":"低髻、银色梅花簪、深红交领长裙、布靴","change_reason":"基础造型","effective_story_node_ids":[]}]}
```
"""


class StorySourceCoverageTests(unittest.TestCase):
    def test_natural_story_detects_both_named_characters(self) -> None:
        names = infer_source_character_candidates(_NATURAL_STORY)
        self.assertIn("沈璃", names)
        self.assertIn("陆沉", names)
        self.assertNotIn("古城", names)
        self.assertNotIn("烽火台", names)

    def test_stage01_rejects_silent_character_omission(self) -> None:
        issues = source_coverage_issues(
            "xiaoduan-story-bible",
            _STAGE01_MISSING_SHENLI,
            _NATURAL_STORY,
        )
        self.assertTrue(any("沈璃" in item and "遗漏" in item for item in issues), issues)
        self.assertFalse(any("陆沉" in item and "遗漏" in item for item in issues), issues)

    def test_stage02_requires_every_confirmed_story_character(self) -> None:
        upstream = _NATURAL_STORY + "\n\n" + _STAGE01_COMPLETE
        issues = source_coverage_issues(
            "xiaoduan-character-assets",
            _STAGE02_ONLY_SHENLI,
            upstream,
        )
        self.assertTrue(any("陆沉" in item and "Stage02" in item for item in issues), issues)

    def test_story_table_parser_handles_compact_semicolon_names(self) -> None:
        names = extract_story_table_names(_STAGE01_COMPLETE, "角色实体表")
        self.assertEqual(names, ["沈璃", "陆沉"])


if __name__ == "__main__":
    unittest.main()
