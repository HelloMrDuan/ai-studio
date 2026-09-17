from __future__ import annotations

import copy
import json
import unittest

from app.v3.llama_structured_output import (
    _drop_invalid_raw_output,
    build_structured_payload,
    llama_response_format,
    llama_transport_schema,
    output_kind_from_schema,
    response_schema_from_call,
)
from app.v3.professional_output_registry import professional_output_json_schema
from app.v3.professional_output_runtime import _runtime_schema_prompt


class LlamaStructuredOutputTests(unittest.TestCase):
    def test_professional_writer_extracts_authoritative_schema(self) -> None:
        schema = professional_output_json_schema("story_bible")
        system = "writer\n\n" + _runtime_schema_prompt("story_bible")
        loaded = response_schema_from_call(system_prompt=system, messages=[])
        self.assertEqual(loaded, schema)
        self.assertEqual(output_kind_from_schema(loaded or {}), "story_bible")

    def test_schema_repair_extracts_same_authoritative_schema(self) -> None:
        schema = professional_output_json_schema("story_bible")
        messages = [{
            "role": "user",
            "content": (
                "OUTPUT_KIND=story_bible\n"
                "VALIDATION_ERRORS=[\"专业输出不是合法 JSON\"]\n"
                f"AUTHORITATIVE_SCHEMA={json.dumps(schema, ensure_ascii=False)}\n"
                "SOURCE=沈川背负古剑。苏瑶手持玉佩。\n"
                "RAW_OUTPUT={\"schema_version\":1,\"document\":\"unfinished"
            ),
        }]
        loaded = response_schema_from_call(system_prompt="repair", messages=messages)
        self.assertEqual(loaded, schema)

    def test_transport_schema_flattens_every_front_half_professional_schema(self) -> None:
        for output_kind in ("story_bible", "character_assets", "visual_assets"):
            with self.subTest(output_kind=output_kind):
                canonical = professional_output_json_schema(output_kind)
                before = copy.deepcopy(canonical)
                projected = llama_transport_schema(canonical)
                encoded = json.dumps(projected, ensure_ascii=False)

                self.assertEqual(canonical, before)
                self.assertNotIn('"$defs"', encoded)
                self.assertNotIn('"definitions"', encoded)
                self.assertNotIn('"$ref"', encoded)
                self.assertNotIn('"const"', encoded)
                self.assertNotIn('"maxLength"', encoded)
                self.assertNotIn('"minLength"', encoded)
                self.assertNotIn('"minItems"', encoded)
                self.assertNotIn('"maxItems"', encoded)
                self.assertNotIn('"pattern"', encoded)
                self.assertEqual(
                    projected["properties"]["output_kind"]["enum"],
                    [output_kind],
                )

    def test_visual_rule_maps_keep_typed_additional_properties(self) -> None:
        schema = llama_transport_schema(
            professional_output_json_schema("visual_assets")
        )
        direction = schema["properties"]["visual_direction"]
        for field in ("character_rules", "environment_rules", "prop_rules"):
            self.assertEqual(
                direction["properties"][field]["additionalProperties"],
                {"type": "string"},
            )

    def test_transport_payload_uses_flat_llama_cpp_response_format(self) -> None:
        canonical = professional_output_json_schema("story_bible")
        projected = llama_transport_schema(canonical)
        payload = build_structured_payload(
            model="qwen3-32b",
            messages=[{"role": "user", "content": "source"}],
            temperature=0.1,
            max_tokens=3200,
            schema=canonical,
        )
        self.assertEqual(
            payload["response_format"],
            {"type": "json_object", "schema": projected},
        )
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["max_tokens"], 3200)

    def test_invalid_truncated_raw_output_is_not_replayed_into_retry(self) -> None:
        prompt = (
            "OUTPUT_KIND=story_bible\n"
            "SOURCE=沈川背负古剑。苏瑶手持玉佩。\n"
            "RAW_OUTPUT={\"schema_version\":1,\"document\":\"unterminated"
        )
        cleaned = _drop_invalid_raw_output(prompt)
        self.assertIn("SOURCE=沈川背负古剑。苏瑶手持玉佩。", cleaned)
        self.assertNotIn("unterminated", cleaned)
        self.assertNotIn("RAW_OUTPUT=", cleaned)
        self.assertIn("invalid_or_truncated", cleaned)

    def test_response_format_helper_does_not_mutate_registry_schema(self) -> None:
        canonical = professional_output_json_schema("story_bible")
        before = copy.deepcopy(canonical)
        response = llama_response_format(canonical)
        response["schema"]["properties"]["document"]["type"] = "integer"
        self.assertEqual(canonical, before)
        self.assertEqual(canonical["properties"]["document"]["type"], "string")


if __name__ == "__main__":
    unittest.main()
