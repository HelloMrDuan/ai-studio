#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/root/autodl-tmp/ai-studio/platform-v2
PY=/root/autodl-tmp/envs/ai-studio-platform-v2/bin/python
RUNTIME_ROOT=/root/autodl-tmp/ai-studio

cd "$ROOT"

echo "================ PERSIST COMFY SAFE POLICY ================"
if [[ -f "$RUNTIME_ROOT/start_comfy.sh" && ! -f "$RUNTIME_ROOT/start_comfy.sh.pre-v3-h3-safe" ]]; then
  cp -a "$RUNTIME_ROOT/start_comfy.sh" "$RUNTIME_ROOT/start_comfy.sh.pre-v3-h3-safe"
fi
install -m 0755 scripts/start_comfy_safe.sh "$RUNTIME_ROOT/start_comfy.sh"
echo "installed: $RUNTIME_ROOT/start_comfy.sh"

echo
echo "================ COMFY REFERENCE PROFILE ================"
"$PY" scripts/v3_faceid_prepare.py
"$PY" scripts/configure_comfy_reference_profile.py --profile faceid

echo
echo "================ TTS ================"
if ! "$PY" -c 'import edge_tts' >/dev/null 2>&1; then
  "$PY" -m pip install --disable-pip-version-check edge-tts
fi
"$PY" scripts/configure_edge_tts_provider.py
bash scripts/start_edge_tts.sh

echo
echo "================ TEMPORAL ================"
if [[ ! -x /root/autodl-tmp/bin/temporal ]]; then
  bash scripts/install_temporal_cli.sh
fi
bash scripts/start_temporal_dev.sh
bash scripts/start_temporal_worker.sh

echo
echo "================ RELOAD V3 PROVIDERS ================"
bash scripts/stop.sh
bash scripts/start.sh

echo
echo "================ REAL TTS ACCEPTANCE ================"
"$PY" scripts/v3_tts_acceptance.py

echo
echo "================ TEMPORAL TRANSPORT ACCEPTANCE ================"
"$PY" scripts/v3_temporal_acceptance.py

echo
echo "================ TEMPORAL BUSINESS E2E ================"
"$PY" scripts/v3_temporal_e2e.py

echo
echo "================ TEMPORAL VISUAL IMAGE -> H3 E2E ================"
"$PY" scripts/v3_temporal_visual_e2e.py --reference-id hero-v1

echo
echo "================ GLOBAL CONTROL ACCEPTANCE ================"
"$PY" scripts/v3_global_acceptance.py --reference-id hero-v1 --timeout 1800
