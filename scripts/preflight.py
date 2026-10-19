#!/usr/bin/env python3
"""部署自检：检查 GPU、磁盘、模型文件、ComfyUI 节点、文字模型、FaceFusion。

    python scripts/preflight.py           # 只检查，不占 GPU
    python scripts/preflight.py --smoke   # 额外实际跑一遍：文字模型 → 出图 → 参考图出图 → 1 秒视频

把输出完整贴回给开发者即可定位问题。
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.workflows import optional_models, required_models  # noqa: E402

EXPECTED_GB = {
    "qwen_image_2.1_turbo_int8_convrot.safetensors": 7.26, "qwen3vl_8b_int8_convrot.safetensors": 9.35,
    "qwen_image_2.1_vae_bf16.safetensors": 0.68, "minimax_h3_fl2va_pruned_int8_convrot.safetensors": 20.97,
    "minimax_h3_ref2va_pruned_int8_convrot.safetensors": 20.97,
    "qwen3vl_32b_minimax_h3_int8_convrot.safetensors": 27.14, "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors": 15.69,
    "minimax_h3_video_vae_int8_convrot.safetensors": 2.81, "minimax_h3_audio_vae_fp32.safetensors": 0.61,
    "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors": 1.96,
    "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors": 1.96,
    "Qwen_Qwen3.6-35B-A3B-Q4_K_M.gguf": 22.28, "Qwen_Qwen3.6-35B-A3B-IQ4_XS.gguf": 19.69,
    "minimax_h3_fl2va_pruned_w6a8.safetensors": 15.98, "minimax_h3_ref2va_pruned_w6a8.safetensors": 15.98,
}
REQUIRED_NODES = ["UNETLoader", "CLIPLoader", "VAELoader", "TextEncodeQwenImage21", "QwenImage21Cache",
                  "ManualSigmas", "CFGGuider", "SamplerCustomAdvanced", "MiniMaxH3ImageToVideo",
                  "MiniMaxH3ReferenceToVideo", "VAEDecodeAudio", "CreateVideo", "SaveVideo", "LoraLoaderModelOnly"]
problems: list[str] = []


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def bad(msg: str, hint: str = "") -> None:
    problems.append(msg)
    print(f"  ✗ {msg}" + (f"\n      → {hint}" if hint else ""))


def warn(msg: str) -> None:
    print(f"  ! {msg}")


def check_gpu() -> None:
    print("[GPU]")
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version,compute_cap",
                              "--format=csv,noheader"], capture_output=True, text=True, timeout=15).stdout.strip()
        ok(out)
        mem = int(out.split(",")[1].strip().split()[0])
        if mem < 30000:
            warn("24GB 显卡：请确认使用 --24g 方案（H3 w6a8 + IQ4_XS），视频建议用草稿 480p、单镜头 ≤8 秒")
        ram = int(open("/proc/meminfo").read().split()[1]) // 1024 // 1024
        (ok if ram >= 60 else warn)(f"内存 {ram}GB" + ("" if ram >= 60 else "，建议 ≥64GB（H3 文本编码器 27GB 需要放在内存里分段加载）"))
    except Exception as exc:  # noqa: BLE001
        bad(f"nvidia-smi 不可用：{exc}")


def check_disk(s) -> None:
    print("[磁盘]")
    for p in (s.base_dir, Path("/")):
        if p.exists():
            u = shutil.disk_usage(p)
            msg = f"{p}: 剩余 {u.free / 1e9:.0f}GB / 共 {u.total / 1e9:.0f}GB"
            (ok if u.free > 20e9 else bad)(msg) if p == s.base_dir else ok(msg)


def check_files(s) -> None:
    print("[程序与模型文件]")
    for label, p in (("ComfyUI", s.comfyui_dir / "main.py"), ("FaceFusion", s.facefusion_dir / "facefusion.py"),
                     ("FaceFusion Python", s.facefusion_python)):
        ok(f"{label}: {p}") if p.exists() else bad(f"{label} 缺失：{p}")
    if s.llm_provider == "local":
        for label, p in (("llama-server", s.llama_server_bin), ("文字模型 GGUF", s.llm_gguf)):
            ok(f"{label}: {p}") if p.exists() else bad(f"{label} 缺失：{p}", "运行 deploy/03_llama.sh 或 download_models.sh")
    for optional, group in ((False, required_models()), (True, optional_models())):
        for folder, names in group.items():
            for name in names:
                p = s.comfyui_dir / "models" / folder / name
                if not p.is_file():
                    (warn if optional else bad)(f"{'可选' if optional else '必需'}模型缺失：{folder}/{name}")
                    continue
                gb = p.stat().st_size / 1e9
                exp = EXPECTED_GB.get(name)
                if exp and gb < exp * 0.98:
                    bad(f"{name} 不完整：{gb:.2f}GB / 应为 {exp}GB", "重新运行 deploy/download_models.sh 续传")
                elif (p.parent / (name + ".aria2")).exists():
                    bad(f"{name} 仍在下载中（存在 .aria2 文件）")
                else:
                    ok(f"{folder}/{name} {gb:.2f}GB")
    try:
        import shutil as sh
        ok("ffmpeg 已安装") if sh.which("ffmpeg") else bad("ffmpeg 未安装", "apt-get install -y ffmpeg")
        fonts = subprocess.run(["fc-list"], capture_output=True, text=True).stdout
        ok("中文字体已安装") if "Noto Sans CJK" in fonts else warn("缺少 Noto Sans CJK 字体，字幕可能显示为方块")
    except Exception:  # noqa: BLE001
        pass


async def check_comfy(s) -> bool:
    print("[ComfyUI]")
    try:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
            stats = (await c.get(s.comfyui_url + "/system_stats")).json()
            info = (await c.get(s.comfyui_url + "/object_info")).json()
    except Exception as exc:  # noqa: BLE001
        bad(f"ComfyUI 未运行（{s.comfyui_url}）：{exc}", "bash deploy/start.sh，日志在 logs/comfyui.log")
        return False
    sysinfo = stats.get("system", {})
    ok(f"ComfyUI {sysinfo.get('comfyui_version', '?')} · Python {sysinfo.get('python_version', '?')[:7]} · "
       f"PyTorch {sysinfo.get('pytorch_version', '?')}")
    for d in stats.get("devices", [])[:1]:
        ok(f"设备 {d.get('name')} · 显存 {d.get('vram_total', 0) / 2**30:.1f}GB")
    missing = [n for n in REQUIRED_NODES if n not in info]
    ok("所需节点齐全") if not missing else bad(f"ComfyUI 缺少节点：{missing}", "升级 ComfyUI：COMFY_REF=master bash deploy/01_comfyui.sh")
    unets = (info.get("UNETLoader", {}).get("input", {}).get("required", {}).get("unet_name") or [[]])[0]
    if isinstance(unets, list) and s.image_unet not in unets:
        bad(f"ComfyUI 看不到 {s.image_unet}（检查 models/diffusion_models）")
    return not missing


async def check_llm(s, smoke: bool) -> None:
    print(f"[文字模型 · {s.llm_provider}]")
    if s.llm_provider == "local" and not smoke:
        ok("本机模式：由 AI Studio 按需启动 llama-server（--smoke 可实际测试）")
        return
    from app import llm
    t = time.time()
    try:
        data = await llm.chat_json("只输出 JSON。", '输出 {"ok": true, "msg": "你好"}', max_tokens=200, retries=1)
        ok(f"调用成功 {time.time() - t:.1f}s：{data}")
    except Exception as exc:  # noqa: BLE001
        bad(f"文字模型调用失败：{exc}", "查看 logs/llama-server.log；或在 .env 改用外部 API")


async def smoke(s) -> None:
    from app import comfy
    from app.gpu import gpu
    from app.workflows import image_workflow, video_i2v_workflow
    out = s.data_dir / "preflight"
    print("[实测 · 出图]")
    try:
        async with gpu.use("comfyui", "自检出图"):
            t = time.time()
            imgs = await comfy.run(image_workflow("一位穿红色风衣的年轻女子站在雨夜街头，电影感", "16:9",
                                                  size=(832, 480), seed=42), out)
            ok(f"文生图 {time.time() - t:.0f}s → {imgs[0]}")
            ref = await comfy.upload_image(imgs[0])
            t = time.time()
            imgs2 = await comfy.run(image_workflow("<image1> 中的女子坐在咖啡馆窗边喝咖啡，保持同一张脸和服装", "16:9",
                                                   [ref], size=(832, 480), seed=7), out)
            ok(f"参考图生图 {time.time() - t:.0f}s → {imgs2[0]}")
            print("[实测 · 视频（1 秒草稿，首次加载模型会比较慢）]")
            t = time.time()
            vids = await comfy.run(video_i2v_workflow("女子转头微笑，轻声说：“你好。”", ref, "16:9", 1.0, "draft", True,
                                                      seed=1), out)
            ok(f"H3 图生视频 {time.time() - t:.0f}s → {vids[0]}")
    except Exception as exc:  # noqa: BLE001
        bad(f"实测失败：{exc}", "查看 logs/comfyui.log 末尾的报错")
    finally:
        await gpu.free_comfy()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="实际跑一次文字/出图/视频")
    args = ap.parse_args()
    s = get_settings()
    print(f"AI Studio 自检 · 配置 {ROOT / '.env'}\n")
    check_gpu()
    check_disk(s)
    check_files(s)
    comfy_ok = await check_comfy(s)
    await check_llm(s, args.smoke)
    if args.smoke and comfy_ok:
        await smoke(s)
        from app.gpu import gpu
        await gpu._stop_llm()
    print("\n" + ("全部通过 ✅" if not problems else f"发现 {len(problems)} 个问题 ❌：\n- " + "\n- ".join(problems)))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    asyncio.run(main())
