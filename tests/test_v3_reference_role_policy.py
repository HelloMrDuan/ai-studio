from pathlib import Path

from app.v3.generation_executor import ReferenceAsset, compile_role_aware_reference_chain
from app.v3.reference_role_policy import install_reference_role_policy, reference_channel


install_reference_role_policy()


def _workflow():
    return {
        "3": {"class_type": "KSampler", "inputs": {"model": ["4", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
        "11": {"class_type": "CLIPVisionLoader", "inputs": {}},
        "12": {"class_type": "IPAdapterUnifiedLoaderFaceID", "inputs": {"model": ["4", 0]}},
        "16": {"class_type": "IPAdapterModelLoader", "inputs": {}},
    }


def _ref(role: str, entity_type: str = "character") -> ReferenceAsset:
    return ReferenceAsset(
        reference_id=f"ref:{role}",
        path=Path(f"/{role}.png"),
        sha256="a" * 64,
        mime_type="image/png",
        entity_id="character-1",
        role=role,
        entity_type=entity_type,
    )


def test_face_anchor_is_faceid_but_costume_is_structure():
    face = _ref("character_face_anchor")
    costume = _ref("character_costume_reference")
    assert reference_channel(face) == "face_identity"
    assert reference_channel(costume) == "character_structure"

    compiled = compile_role_aware_reference_chain(
        _workflow(),
        ["face.png", "costume.png"],
        [face, costume],
    )
    node_types = [node.get("class_type") for node in compiled.values()]
    assert node_types.count("IPAdapterFaceID") == 1
    assert node_types.count("IPAdapterAdvanced") == 1
    assert compiled["3"]["inputs"]["model"] != ["4", 0]


def test_turnaround_is_structural_not_face_embedding():
    turnaround = _ref("character_turnaround")
    assert reference_channel(turnaround) == "character_structure"
    compiled = compile_role_aware_reference_chain(
        _workflow(),
        ["turnaround.png"],
        [turnaround],
    )
    node_types = [node.get("class_type") for node in compiled.values()]
    assert "IPAdapterFaceID" not in node_types
    assert node_types.count("IPAdapterAdvanced") == 1


def test_unknown_character_image_never_silently_promotes_to_faceid():
    unknown = _ref("legacy_character_picture")
    assert reference_channel(unknown) == "character_structure"
