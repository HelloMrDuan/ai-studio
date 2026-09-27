from __future__ import annotations

import re

from .contracts import ResolvedField, ResolutionSource


_EXPLICIT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Japanese", (r"日本(?:人|籍|少年|少女|学生|警察|武士)", r"来自日本", r"日籍")),
    ("American", (r"美国(?:人|籍|少年|少女|学生|警察|军人)", r"来自美国", r"美籍")),
    ("Chinese", (r"中国(?:人|籍|少年|少女|学生|警察|军人)", r"来自中国", r"中籍")),
)

_CONTEXT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Japanese", (r"东京", r"大阪", r"京都", r"神社", r"涩谷", r"新宿")),
    ("American", (r"纽约", r"曼哈顿", r"洛杉矶", r"华盛顿", r"芝加哥")),
    ("Chinese", (r"北京", r"上海", r"江南", r"长安", r"故宫", r"中式", r"华夏")),
)


class ContextResolver:
    """Resolve story-scope visual context once, then persist/lock it.

    This deterministic resolver only handles high-signal cultural context. More
    nuanced fields can later be supplied by a structured LLM resolver, but the
    precedence and provenance contract remain the same.
    """

    def __init__(self, default_cultural_context: str = "Chinese") -> None:
        value = str(default_cultural_context or "").strip()
        if not value:
            raise ValueError("default_cultural_context is required")
        self.default_cultural_context = value

    @staticmethod
    def _match(text: str, table: tuple[tuple[str, tuple[str, ...]], ...]) -> tuple[str, str] | None:
        for value, patterns in table:
            for pattern in patterns:
                match = re.search(pattern, text)
                if match:
                    return value, match.group(0)
        return None

    def resolve_cultural_context(
        self,
        source_text: str,
        *,
        user_override: str | None = None,
        previous_locked: ResolvedField | None = None,
    ) -> ResolvedField:
        if previous_locked is not None and previous_locked.locked:
            return previous_locked

        override = str(user_override or "").strip()
        if override:
            return ResolvedField(
                value=override,
                source=ResolutionSource.user_override,
                confidence=1.0,
                locked=True,
            )

        text = str(source_text or "").strip()
        explicit = self._match(text, _EXPLICIT_PATTERNS)
        if explicit:
            value, evidence = explicit
            return ResolvedField(
                value=value,
                source=ResolutionSource.script_explicit,
                confidence=1.0,
                locked=True,
                evidence_refs=(evidence,),
            )

        inferred = self._match(text, _CONTEXT_PATTERNS)
        if inferred:
            value, evidence = inferred
            return ResolvedField(
                value=value,
                source=ResolutionSource.context_inferred,
                confidence=0.9,
                locked=True,
                evidence_refs=(evidence,),
            )

        return ResolvedField(
            value=self.default_cultural_context,
            source=ResolutionSource.project_default,
            confidence=0.5,
            locked=True,
        )
