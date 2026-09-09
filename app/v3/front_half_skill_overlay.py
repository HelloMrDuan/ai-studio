from __future__ import annotations

from typing import Any, Callable


# These are independently written compatibility constraints derived from the
# reusable-asset / creative-skill discipline studied in waoowaoo. They are not
# copied implementation code. The block deliberately states that it adds no new
# artifact IDs or completion gates so the existing Director Skill contracts stay
# authoritative.
_STAGE_GUIDANCE = {
    "chuanzhang-chuangzuo-v1": r"""

## 小段映画前半段质量约束（兼容附录，不新增交付物/完成条件）

本附录只约束现有剧本成果的质量，不创建新的 artifact、requirement、阶段或状态机。
- 严格保持用户原故事的事实与因果，不补造未提供的剧情结果。
- 场景拆分必须能看出“谁、在哪里、做什么、为什么发生下一步”，避免只有一句概括。
- 对后续会重复出现的人物、地点、关键道具，正文中要保留稳定名称与可追踪身份；不要在不同场景随意改名。
- 把长期稳定事实与镜头瞬时状态分开：身份/服装/空间结构/道具结构属于稳定事实，表情/动作/机位/光线变化属于镜头状态。
- 给后续角色、视觉和分镜阶段留下可执行的场景、动作、对白/旁白及关键道具依据，而不是提前写图片提示词。
""",
    "ai-studio-character-design": r"""

## 小段映画角色资产约束（兼容附录，不新增交付物/完成条件）

本附录只提高现有角色设计成果质量，不创建新的 artifact、requirement、阶段或状态机。
- 角色资产必须描述可跨镜头复用的稳定身份：脸部结构、发型发色、体型、肤色、服装、鞋履、配饰与固定配色。
- 稳定身份描述中不要混入某个镜头的表情、姿势、动作、机位、背景或临时道具。
- 性格、动机、关系与剧情职责可以保留，但要与“稳定视觉身份”分栏/分段，避免后续图片模型把性格描述误当外观。
- 上游未确认的外观细节必须明确为“未指定/待设计”，不得伪装成原文事实。
- 同一人物只维护一个稳定角色身份；剧情造成长期外观变化时，记录为新的版本状态，而不是静默覆盖旧身份。
- 不要求用户必须上传真人参考图。角色设计完成后，系统可依据已确认稳定身份自动生成4:3角色参考图候选，再由用户采用。
""",
    "ai-studio-visual-design": r"""

## 小段映画可复用视觉资产约束（兼容附录，不新增交付物/完成条件）

本附录只提高现有视觉设计成果质量，不创建新的 artifact、requirement、阶段或状态机。
- 只把需要跨镜头保持一致的角色、地点/场景、关键道具定义成可复用资产；一次性动作或纯镜头效果不要升级为资产身份。
- 地点/场景的稳定描述应包含空间结构、主要材质、固定陈设、前中后景关系和稳定辨识锚点；不要写某个镜头的演员动作。
- 道具的稳定描述应包含完整轮廓、结构、材质、颜色、纹样和长期磨损特征；不要包含握持姿势或某一帧位置。
- 项目整体画风、色彩和摄影方向与资产身份分开保存；画风可以影响生成，但不能改写角色脸、服装、场景结构或道具结构。
- 每个可复用资产的稳定设计必须足够明确，使后续系统能自动生成4:3参考图候选并让用户评审/采用。
- 视觉阶段不负责把剧情概括成最终镜头提示词；④分镜必须基于剧本事实、连续性状态和这些稳定资产另行编译镜头合同。
""",
}


class FrontHalfSkillOverlay:
    """Append quality guidance to existing mature Skills without replacing them."""

    def __init__(self, director: Any) -> None:
        self.director = director
        self._original: Callable[[str], str] | None = None

    def install(self) -> None:
        if getattr(self.director, "_xiaoduan_front_half_overlay_installed", False):
            return
        original = getattr(self.director, "_skill_md", None)
        if not callable(original):
            raise RuntimeError("原工作台缺少 Skill 读取入口，不能安装前半段质量约束")
        self._original = original

        def wrapped(skill_name: str) -> str:
            base = str(original(skill_name) or "")
            guidance = _STAGE_GUIDANCE.get(str(skill_name or "").strip())
            if not guidance:
                return base
            marker = "## 小段映画"
            if marker in base:
                return base
            return base.rstrip() + "\n" + guidance.strip() + "\n"

        self.director._skill_md = wrapped
        self.director._xiaoduan_front_half_overlay_installed = True


__all__ = ["FrontHalfSkillOverlay"]
