from __future__ import annotations

import asyncio
import json
import types
import unittest

from app.v3.stage04_evidence_prompt_guard import (
    guard_stage04_prompt,
    install_stage04_evidence_prompt_guard,
)


class Stage04EvidencePromptGuardTests(unittest.TestCase):
    def test_missing_beat_prompt_keeps_exact_evidence_and_drops_adjacent_facts(self) -> None:
        prompt = (
            '=== PREVIOUS_ACCEPTED_SHOT ===\n'
            + json.dumps({'video_end_state': '巨石坠落，沈川拔剑挡在苏瑶身前。'}, ensure_ascii=False)
            + '\n\n=== TARGET_BEAT ===\n'
            + json.dumps({
                'order': 5,
                'summary': '两人同时愣住，随后风雪加剧。',
                'state_change': '风雪越来越大',
                'allowed_source_evidence_ids': ['E005'],
            }, ensure_ascii=False)
            + '\n\n=== ALLOWED_EVIDENCE_ANCHORS ===\n'
            + json.dumps([{'id': 'E005', 'text': '两人同时愣住。'}], ensure_ascii=False)
            + '\n\n=== NEXT_BEAT_PREVIEW_DO_NOT_CONSUME ===\n'
            + json.dumps({'summary': '风雪越来越大。'}, ensure_ascii=False)
        )
        system, guarded = guard_stage04_prompt(
            'studio_stage04_v2392_missing_beat_completion_qwen32b',
            'system',
            prompt,
        )
        self.assertIn('RUNTIME_EVIDENCE_GUARD', system)
        self.assertIn('两人同时愣住。', guarded)
        self.assertIn('"order":5', guarded)
        self.assertIn('"allowed_source_evidence_ids":["E005"]', guarded)
        self.assertNotIn('巨石坠落', guarded)
        self.assertNotIn('沈川拔剑', guarded)
        self.assertNotIn('风雪加剧', guarded)
        self.assertNotIn('风雪越来越大', guarded)

    def test_directional_repair_redacts_failed_fields_and_adjacent_context(self) -> None:
        prompt = (
            '=== EXACT_SELECTED_EVIDENCE ===\n'
            + json.dumps([{'id': 'E005', 'text': '两人同时愣住。'}], ensure_ascii=False)
            + '\n\n=== LOCKED_COVERED_BEATS ===\n'
            + json.dumps([{
                'order': 5,
                'summary': '风雪加剧。',
                'allowed_source_evidence_ids': ['E005'],
            }], ensure_ascii=False)
            + '\n\n=== CURRENT_SHOT ===\n'
            + json.dumps({
                'summary': '风雪加剧。',
                'action': '两人同时愣住。',
                'video_start_state': '巨石坠落，沈川拔剑挡在苏瑶身前。',
                'representative_state': '两人同时愣住。',
                'video_end_state': '两人同时愣住。',
                'video_prompt': '起始状态：巨石坠落。',
                'covered_beat_orders': [5],
                'source_evidence_ids': ['E005'],
            }, ensure_ascii=False)
            + '\n\n=== FAILED_FIELDS ===\n'
            + json.dumps(['summary', 'action', 'video_start_state', 'representative_state', 'video_end_state'], ensure_ascii=False)
            + '\n\n=== FAILED_AUDIT_RULES ===\n[]'
            + '\n\n=== PREVIOUS_ACCEPTED_SHOT ===\n'
            + json.dumps({'video_end_state': '沈川挡剑。'}, ensure_ascii=False)
            + '\n\n=== NEXT_CURRENT_SHOT_CONTEXT_ONLY ===\n'
            + json.dumps({'summary': '下一镜头。'}, ensure_ascii=False)
            + '\n\n=== NEXT_BEAT_PREVIEW_DO_NOT_CONSUME ===\n'
            + json.dumps({'summary': '风雪越来越大。'}, ensure_ascii=False)
        )
        _system, guarded = guard_stage04_prompt(
            'studio_stage04_v2383_evidence_locked_repair_qwen32b',
            'system',
            prompt,
        )
        self.assertIn('两人同时愣住。', guarded)  # exact evidence remains
        self.assertNotIn('巨石坠落', guarded)
        self.assertNotIn('沈川挡剑', guarded)
        self.assertNotIn('下一镜头', guarded)
        self.assertNotIn('风雪越来越大', guarded)
        self.assertNotIn('风雪加剧', guarded)
        self.assertIn('"order":5', guarded)

    def test_runtime_hook_guards_only_targeted_phases(self) -> None:
        seen: list[dict] = []

        async def fake_qwen(*args, **kwargs):
            seen.append(dict(kwargs))
            return {'ok': True}

        runtime = types.SimpleNamespace(_qwen=fake_qwen)
        result = install_stage04_evidence_prompt_guard(runtime)
        self.assertEqual(result['status'], 'installed')

        async def run() -> None:
            await runtime._qwen(
                phase='studio_stage04_v2392_missing_beat_completion_qwen32b',
                system_prompt='system',
                prompt=(
                    '=== PREVIOUS_ACCEPTED_SHOT ===\n{"summary":"旧事实"}\n\n'
                    '=== TARGET_BEAT ===\n{"order":5,"summary":"越界事实","allowed_source_evidence_ids":["E5"]}\n\n'
                    '=== ALLOWED_EVIDENCE_ANCHORS ===\n[{"id":"E5","text":"当前证据"}]\n\n'
                    '=== NEXT_BEAT_PREVIEW_DO_NOT_CONSUME ===\n{"summary":"未来事实"}'
                ),
            )
            await runtime._qwen(
                phase='unrelated_phase',
                system_prompt='system',
                prompt='UNCHANGED',
            )

        asyncio.run(run())
        self.assertEqual(len(seen), 2)
        self.assertNotIn('旧事实', seen[0]['prompt'])
        self.assertNotIn('越界事实', seen[0]['prompt'])
        self.assertNotIn('未来事实', seen[0]['prompt'])
        self.assertIn('当前证据', seen[0]['prompt'])
        self.assertEqual(seen[1]['prompt'], 'UNCHANGED')


if __name__ == '__main__':
    unittest.main()
