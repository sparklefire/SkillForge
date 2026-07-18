#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="$ROOT/outputs/cache/tessdata"
COMMIT="87416418657359cb625c412a48b6e1d6d41c29bd"
BASE="https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/$COMMIT"
OFFLINE=false

case "${1:-}" in
  "") ;;
  --offline) OFFLINE=true ;;
  *)
    echo "用法: bash scripts/setup_ocr_languages.sh [--offline]" >&2
    exit 2
    ;;
esac

if ! command -v tesseract >/dev/null 2>&1; then
  echo "缺少Tesseract运行时，无法执行本地PDF OCR" >&2
  exit 1
fi

mkdir -p "$CACHE"
chmod 700 "$ROOT/outputs/cache" "$CACHE"

download_and_verify() {
  local language="$1"
  local expected="$2"
  local target="$CACHE/$language.traineddata"
  local actual=""
  if [[ -f "$target" ]]; then
    actual="$(LC_ALL=C openssl dgst -sha256 "$target" | awk '{print $NF}')"
  fi
  if [[ "$actual" != "$expected" ]]; then
    if [[ "$OFFLINE" == true ]]; then
      echo "$language OCR数据缺失或哈希无效；离线模式拒绝下载" >&2
      exit 1
    fi
    rm -f "$target"
    curl --fail --location --silent --show-error \
      "$BASE/$language.traineddata" -o "$target"
    actual="$(LC_ALL=C openssl dgst -sha256 "$target" | awk '{print $NF}')"
  fi
  if [[ "$actual" != "$expected" ]]; then
    rm -f "$target"
    echo "$language OCR数据哈希校验失败" >&2
    exit 1
  fi
  chmod 600 "$target"
}

download_and_verify \
  "chi_sim" \
  "a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730"
download_and_verify \
  "eng" \
  "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"

printf 'OCR_LANGUAGES_OK commit=%s languages=chi_sim+eng offline=%s\n' "$COMMIT" "$OFFLINE"
