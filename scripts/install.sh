#!/usr/bin/env bash
set -u

ROOT=/root/autodl-tmp/ai-studio/platform-v2
VENV=/root/autodl-tmp/envs/ai-studio-platform-v2

if [[ ! -d "$ROOT" ]]; then
  echo "项目目录不存在：$ROOT"
else
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip
  "$VENV/bin/pip" install -r "$ROOT/requirements.txt"

  if [[ ! -f "$ROOT/.env" ]]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
  fi

  mkdir -p /root/autodl-tmp/ai-studio/logs
  chmod +x "$ROOT"/scripts/*.sh

  echo
  echo "安装完成。请先检查配置："
  echo "$ROOT/.env"
fi
