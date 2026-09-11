from __future__ import annotations

import unittest

from app.v3.professional_source_grounding import (
    authoritative_message_source,
    ground_source_evidence,
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


if __name__ == "__main__":
    unittest.main()
