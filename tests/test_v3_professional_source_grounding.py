from __future__ import annotations

import unittest

from app.v3.professional_source_grounding import (
    authoritative_message_source,
    ground_source_evidence,
    install_professional_source_grounding,
)


_SOURCE = """《青云山的第一场雪》

青云山迎来入冬后的第一场大雪。

17岁的少年沈川独自沿着覆雪的山间石阶前行。他身穿深蓝色古式长袍，黑发束起，背负一柄古剑。古剑使用乌木剑鞘，剑鞘上有暗银色纹路。

走到悬崖附近时，沈川发现少女苏瑶独自站在雪中。苏瑶身穿浅青色古式长裙，手中握着一枚玉佩。

突然，上方岩壁松动，一块巨石向苏瑶坠落。

沈川迅速拔剑挡在苏瑶身前。

古剑出鞘的一瞬间，剑身上的暗银纹路开始发光。

与此同时，苏瑶手中的玉佩也亮起了完全相同的纹路。

两人同时愣住。

风雪越来越大。""".strip()


class ProfessionalSourceGroundingTests(unittest.TestCase):
    def test_reads_real_director_authoritative_current_user_marker(self) -> None:
        generated_history = "助手曾错误总结：古剑来自师父，玉佩来自宗门。"
        messages = [{
            "role": "user",
            "content": f"""=== SOURCE FILES ===
<skill text>

=== AUTHORITATIVE PREVIOUS CONFIRMED STAGE TEXT ===
<none>

=== GENERATED CURRENT-STAGE HISTORY NOTICE ===
generated history is not authoritative

=== RECENT CURRENT-STAGE HISTORY ===
{generated_history}

=== AUTHORITATIVE CURRENT USER MESSAGE ===
{_SOURCE}
""",
        }]
        source = authoritative_message_source(messages)
        self.assertEqual(source, _SOURCE)
        self.assertIn("背负一柄古剑", source)
        self.assertIn("手中握着一枚玉佩", source)
        self.assertNotIn("来自师父", source)
        self.assertNotIn("来自宗门", source)

    def test_regenerate_recovers_original_user_story_but_not_assistant_history(self) -> None:
        generated_history = "故事生产圣经：沈川来自某宗门，苏瑶是宗门弟子。"
        messages = [{
            "role": "user",
            "content": f"""=== SOURCE FILES ===
<skill text>

=== RECENT CURRENT-STAGE HISTORY ===
user: {_SOURCE}

assistant: {generated_history}

user: 重新生成

=== AUTHORITATIVE CURRENT USER MESSAGE ===
重新生成
""",
        }]
        source = authoritative_message_source(messages)
        self.assertIn(_SOURCE, source)
        self.assertIn("重新生成", source)
        self.assertNotIn("来自某宗门", source)
        self.assertNotIn("宗门弟子", source)

    def test_paraphrased_prop_evidence_is_bound_to_exact_source_span(self) -> None:
        payload = {
            "output_kind": "story_bible",
            "characters": [],
            "locations": [],
            "props": [
                {
                    "name": "古剑",
                    "source_evidence": "沈川携带着一柄古剑",
                },
                {
                    "name": "玉佩",
                    "source_evidence": "苏瑶拿着一块玉佩",
                },
            ],
        }
        ground_source_evidence(payload, _SOURCE)
        by_name = {row["name"]: row["source_evidence"] for row in payload["props"]}

        self.assertIn(by_name["古剑"], _SOURCE)
        self.assertIn("古剑", by_name["古剑"])
        self.assertIn(by_name["玉佩"], _SOURCE)
        self.assertIn("玉佩", by_name["玉佩"])
        self.assertNotEqual(by_name["古剑"], "沈川携带着一柄古剑")
        self.assertNotEqual(by_name["玉佩"], "苏瑶拿着一块玉佩")

    def test_invented_identity_is_not_silently_grounded(self) -> None:
        payload = {
            "output_kind": "story_bible",
            "characters": [],
            "locations": [],
            "props": [
                {
                    "name": "神秘戒指",
                    "source_evidence": "沈川还戴着一枚神秘戒指",
                },
            ],
        }
        ground_source_evidence(payload, _SOURCE)
        self.assertEqual(
            payload["props"][0]["source_evidence"],
            "沈川还戴着一枚神秘戒指",
        )
        self.assertNotIn("神秘戒指", _SOURCE)

    def test_soft_pack_current_user_input_is_also_authoritative(self) -> None:
        messages = [{
            "role": "user",
            "content": f"""=== CURRENT TARGET ===
{{}}

=== CURRENT USER INPUT ===
{_SOURCE}

=== CONFIRMED UPSTREAM FACTS ===
<none>
""",
        }]
        self.assertEqual(authoritative_message_source(messages), _SOURCE)

    def test_typed_runtime_does_not_require_markdown_to_echo_machine_entities(self) -> None:
        from app.v3 import professional_output_runtime as runtime

        install_professional_source_grounding()
        payload = {
            "output_kind": "story_bible",
            "document": "# 故事说明\n雪山风暴中，两名少年角色因异常共鸣相遇。",
            "characters": [
                {"name": "沈川", "source_evidence": "沈川携带着一柄古剑"},
                {"name": "苏瑶", "source_evidence": "苏瑶拿着一块玉佩"},
            ],
            "locations": [
                {"name": "青云山", "source_evidence": "故事发生在青云山"},
            ],
            "props": [
                {"name": "古剑", "source_evidence": "沈川携带着一柄古剑"},
                {"name": "玉佩", "source_evidence": "苏瑶拿着一块玉佩"},
            ],
        }
        issues = runtime.validate_professional_output(
            payload,
            source_text=_SOURCE,
            required_names={"characters": ["沈川", "苏瑶"]},
        )
        self.assertEqual(issues, [])
        self.assertEqual(runtime._document_issues(payload), [])

        rows = runtime._entity_rows(payload, payload["document"])
        self.assertEqual(
            {row["name"] for row in rows},
            {"沈川", "苏瑶", "青云山", "古剑", "玉佩"},
        )
        self.assertTrue(all(row["evidence_quote"] in _SOURCE for row in rows))


if __name__ == "__main__":
    unittest.main()
