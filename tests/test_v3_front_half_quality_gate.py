from __future__ import annotations

import unittest

from app.v3.front_half_quality_gate import (
    parse_visual_direction_block,
    validate_front_half_output,
)


class FrontHalfQualityGateTests(unittest.TestCase):
    def test_story_bible_requires_all_ten_fact_groups(self) -> None:
        good = """
# 故事生产圣经
## 故事定位
这是一个完整短篇故事定位，目标时长约三分钟。这里补充足够的生产说明以形成稳定事实源。
## 不可篡改事实
沈川与苏瑶进入青云山，关键因果保持原文。
## 世界观与时间线
古代东方仙侠世界；事件按第一夜、第二日发生。
## 剧情节点
N01 进入山门；N02 发现石门；N03 作出选择。每个节点写明进入条件、事件、结果和下一节点。
## 角色实体表
沈川；苏瑶。角色均使用稳定唯一名称并关联剧情节点和原文证据。
## 地点实体表
青云山；旧道观。地点关联剧情节点，未知视觉细节待设计。
## 道具实体表
乌木剑鞘；青玉坠。记录首次出现与后续状态变化。
## 对白与旁白事实
保留原文关键对白，其他叙述允许压缩但不改事实。
## 连续性事件
记录服装、天气、持有物和时间变化，供后续镜头继承。
## 创作计划
冲突逐步升级，高潮放在石门开启，计划不覆盖事实层。
""" + ("稳定生产事实。" * 30)
        self.assertTrue(validate_front_half_output("xiaoduan-story-bible", good)["valid"])
        bad = good.replace("## 地点实体表", "## 空间内容")
        result = validate_front_half_output("xiaoduan-story-bible", bad)
        self.assertFalse(result["valid"])
        self.assertTrue(any("地点实体表" in item for item in result["issues"]))

    def test_character_contract_requires_gender_and_identity_fields(self) -> None:
        good = """
## 角色资产：苏瑶
- 稳定身份：青云山弟子
- 性别呈现：女性
- 年龄感：16-17岁
- 脸部结构与五官：清秀少女脸，杏眼
- 发型：黑色长发以玉簪挽起
- 发色：黑色
- 肤色：偏白
- 体型：纤细匀称
- 身高感：中等偏高
- 服装：浅青色交领长裙，白色薄披风
- 鞋履：浅色布靴
- 固定身份锚点：年龄、脸、发型、浅青交领长裙
- 允许变化项：表情、姿势、轻微污损
- 形象版本：v1 默认造型
- change_reason：故事开场默认状态
- 参考图生成要求：稳定身份，不带剧情动作和场景背景
- 原文证据：原文明确苏瑶为少女并写明服装
- 设计补全来源：未指定五官细节由角色设计补全
```appearance-versions-json
{
  "versions": [
    {
      "appearance_id": "default",
      "name": "默认造型",
      "stable_design": "16-17岁女性，黑色长发以玉簪挽起，纤细匀称，浅青色交领长裙、白色薄披风、浅色布靴",
      "change_reason": "故事开场默认状态",
      "effective_story_node_ids": []
    }
  ]
}
```
"""
        self.assertTrue(validate_front_half_output("xiaoduan-character-assets", good)["valid"])
        bad = good.replace("- 性别呈现：女性\n", "")
        result = validate_front_half_output("xiaoduan-character-assets", bad)
        self.assertFalse(result["valid"])
        self.assertTrue(any("性别" in item for item in result["issues"]))

    def test_visual_direction_is_strict_machine_contract(self) -> None:
        good = """
## 项目视觉圣经
- 媒介与技法：电影写实摄影，细腻自然肤质与真实布料。
- 项目色板：主色 #203A5F，辅色 #DDE6EA，强调色 #6F8A63，背景 #ECE8DF。
- 边缘与线条：人物轮廓自然清晰，背景边缘柔和，不使用漫画粗线。
- 完成度与细节密度：人物高细节，服装中高纹理，远景适度降细节。
- 禁止漂移：人物性别、年龄、固定发型、服装版本、地点结构不得改变。
```visual-direction-json
{
  "world_style": "东方仙侠",
  "culture": "古代中国文化语境",
  "era": "古代",
  "art_style": "电影级写实摄影",
  "character_rules": {"identity": "东亚人物特征，身份与形象版本稳定"},
  "environment_rules": {"space": "古代中式建筑与自然山地结构"},
  "prop_rules": {"design": "材质、轮廓和纹样跨镜头稳定"},
  "negative_constraints": ["western fantasy identity drift", "modern clothing"]
}
```
## 地点资产：青云山道观
- 空间边界：山腰平台与道观院墙构成边界
- 布局：山门、前院、正殿沿中轴排列
- 材质：青砖、灰瓦、旧木
- 前景：石阶
- 中景：山门和石灯
- 后景：正殿与山体
- 固定视觉锚点：至少三个；石阶、飞檐山门、成对石灯
- 可变化状态：天气、时段、人物位置
- 参考图生成要求：只表现稳定空间身份
## 道具资产：青玉坠
- 完整轮廓：椭圆玉坠
- 比例：掌心大小
- 结构：玉坠、系绳
- 材质：青玉
- 颜色：青绿色
- 尺度关系：掌心大小
- 剧情功能：苏瑶的身份物件
- change_reason：默认无版本变化
- 参考图生成要求：白底完整展示道具本体
"""
        result = validate_front_half_output("xiaoduan-visual-assets", good)
        self.assertTrue(result["valid"], result["issues"])
        direction = parse_visual_direction_block(good)
        self.assertEqual(direction["world_style"], "东方仙侠")
        self.assertEqual(direction["culture"], "古代中国文化语境")
        self.assertIn("western fantasy identity drift", direction["negative_constraints"])

        bad = good.replace("#203A5F", "深蓝").replace("#DDE6EA", "浅灰")
        result = validate_front_half_output("xiaoduan-visual-assets", bad)
        self.assertFalse(result["valid"])
        self.assertTrue(any("HEX" in item for item in result["issues"]))

    def test_visual_direction_rejects_missing_structured_fields(self) -> None:
        bad = """```visual-direction-json
{"world_style":"东方仙侠","culture":"中国","era":"古代","art_style":"写实"}
```"""
        with self.assertRaises(ValueError):
            parse_visual_direction_block(bad)


if __name__ == "__main__":
    unittest.main()
