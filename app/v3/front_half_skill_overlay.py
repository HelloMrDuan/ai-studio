from __future__ import annotations

from typing import Any, Callable


# Independent compatibility guidance derived from the reusable-asset discipline
# studied in waoowaoo. No upstream implementation is copied. The original
# Xiaoduan Director Skills remain authoritative for artifact IDs and completion
# gates; these rules make their existing outputs production-grade inputs for the
# versioned asset graph.
_STAGE_GUIDANCE = {
    "chuanzhang-chuangzuo-v1": r"""

## 小段映画前半段质量约束
## 小段映画 · 资产驱动剧本规则（兼容现有交付物）

本规则不新增交付物/完成条件，只提高现有剧本/交接内容的结构质量。严格保持用户原故事的事实与因果，不补造未提供的剧情结果。

### 1. 剧情结构必须可生产
现有剧本成果中必须能明确识别：
- 故事目标与核心冲突；
- 按发生顺序排列的剧情节点；
- 每个节点中的人物、地点、关键道具；
- 动作、对白/旁白、因果关系与进入下一节点的原因；
- 原故事没有明确给出的事实不得伪装成原文事实。

禁止只输出“某人在某地做某事”这一类一句话概括后就结束阶段。对后续真正需要拍摄的内容，必须展开到足以让角色设计、视觉设计和分镜继续工作的粒度。

### 2. 先确定身份，再描述镜头状态
对重复出现的人物、地点、关键道具使用稳定且唯一的名称。把两类信息分开：
- **稳定事实**：人物身份/长期外观，地点空间结构，道具结构与材质；
- **剧情状态**：某一时刻的表情、动作、位置、损伤、天气、灯光变化。

不要把镜头机位、景别、摄影参数写进稳定身份；这些属于④分镜。①剧本只提供剧情事实和动作依据，不是提前写图片提示词，也不提前写最终 image_prompt。

### 3. 给下游留下可追踪依据
现有阶段交接中应清楚列出：
- 主要人物清单及剧情职责；
- 主要地点/场景清单；
- 关键道具清单；
- 每个剧情节点使用了哪些人物/地点/道具；
- 旁白、对白和必须保留的剧情事实。

这不是额外文件，而是现有剧本成果应具备的内容完整度。
""",
    "ai-studio-character-design": r"""

## 小段映画角色资产约束
## 小段映画 · 可复用角色资产规则（兼容现有交付物）

本规则不新增交付物/完成条件。参考成熟影视资产库“角色可拥有可修改形象版本、渲染候选再选择正式版本”的做法，但继续使用小段映画现有角色阶段、候选和版本系统。

### 1. 每个角色必须有独立稳定身份
现有角色成果中，每个需要跨镜头出现的角色必须独立描述以下稳定信息：
- 姓名/身份/年龄感；
- 脸部结构与主要辨识特征；
- 发型、发色；
- 体型、身高感、肤色；
- 常态服装分层；
- 鞋履、固定配饰；
- 固定主色/辅色；
- 原文明确的长期特征。

信息缺失时写“未指定/待设计”，不得伪装成原文事实。需要设计的部分可以提出设计方案，但必须与“原文事实”分开。

### 2. 稳定身份和剧情形象变化分开
稳定身份不要混入某个镜头的表情、姿势、动作、机位、背景或临时道具。一次性的泥污、受伤、湿衣等属于剧情状态，不属于基础身份。

如果剧情导致长期形象变化，应作为同一角色的“新形象版本/变化原因”保留，而不是静默覆盖基础身份。

### 3. 角色文本必须能直接生成身份参考图
角色设计结束时，稳定身份描述必须具体到能生成一张4:3角色身份参考图：左侧同一角色脸部近景，右侧同一角色无遮挡全身，两侧脸、发型、体型、服装、鞋履和配色一致。

用户不必上传参考图。系统后续会根据已确认稳定身份生成候选；候选只有人工采用后才成为下游正式参考资产。
""",
    "ai-studio-visual-design": r"""

## 小段映画可复用视觉资产约束
## 小段映画 · 场景/道具资产规则（兼容现有交付物）

本规则不新增交付物/完成条件，把现有视觉阶段从“泛化美术描述”收紧为可复用视觉资产设计。项目整体创意方向与单个资产身份必须分开。一次性动作或纯镜头效果不要升级为资产身份。

### 1. 项目创意方向
现有视觉成果应明确但不要覆盖资产身份：
- 整体画风/写实程度；
- 时代与世界观视觉基调；
- 主色体系、光影、质感；
- 摄影视觉倾向。

创意方向只能影响表现方式，不能改变角色已经确认的脸、服装，也不能随意改变场景/道具结构。

### 2. 每个重复地点必须成为独立场景资产
稳定场景设计至少覆盖：
- 空间边界与布局；
- 主要建筑/地形结构；
- 主要材质；
- 固定陈设；
- 前景/中景/后景关系；
- 至少三个跨镜头可辨认的固定锚点。

天气、人物动作、一次性爆炸/破坏、镜头角度属于镜头状态，不属于场景基础身份。

### 3. 每个关键道具必须成为独立道具资产
稳定道具设计至少覆盖：
- 完整轮廓和比例；
- 结构组成；
- 材质、颜色、纹样；
- 固定磨损或辨识特征；
- 尺寸感/与人物的尺度关系（原文有依据时）。

握持姿势、在画面中的位置、一次性破损状态不得写进基础道具身份。

### 4. 资产设计必须可生成参考图
每个需要跨镜头复用的场景/道具，稳定设计应足够具体，使系统能自动生成4:3参考图候选，并由用户采用正式版本。

③视觉不提前替④分镜决定具体机位、景别和动作。④必须消费①已确认剧情事实 + ②已确认角色资产 + ③已确认场景/道具资产后再生成镜头合同。
""",
}


class FrontHalfSkillOverlay:
    """Append asset-driven production guidance without replacing mature Skills."""

    def __init__(self, director: Any) -> None:
        self.director = director
        self._original: Callable[[str], str] | None = None

    def install(self) -> None:
        if getattr(self.director, "_xiaoduan_front_half_overlay_installed", False):
            return
        original = getattr(self.director, "_skill_md", None)
        if not callable(original):
            raise RuntimeError("原工作台缺少创作能力读取入口，不能安装前半段资产规则")
        self._original = original

        def wrapped(skill_name: str) -> str:
            base = str(original(skill_name) or "")
            guidance = _STAGE_GUIDANCE.get(str(skill_name or "").strip())
            if not guidance:
                return base
            marker = "## 小段映画 ·"
            if marker in base:
                return base
            return base.rstrip() + "\n\n" + guidance.strip() + "\n"

        self.director._skill_md = wrapped
        self.director._xiaoduan_front_half_overlay_installed = True


__all__ = ["FrontHalfSkillOverlay"]
