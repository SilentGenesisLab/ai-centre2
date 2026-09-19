#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/donxu/ai-centre}"
WHEEL_PYTHON="${WHEEL_PYTHON:-$PROJECT_ROOT/.venv-control/bin/python}"
PATCH_FILE="$PROJECT_ROOT/patches/vllm-omni-v0.26.0-voxcpm2-stability.patch"
OUTPUT_DIR="$PROJECT_ROOT/vendor/wheels"
OFFICIAL_WHEEL="${OFFICIAL_WHEEL:-$OUTPUT_DIR/vllm_omni-0.26.0-py3-none-any.whl}"
BUILD_DIR="$(mktemp -d /tmp/vllm-omni-sligen1.XXXXXX)"

cleanup() {
  case "$BUILD_DIR" in
    /tmp/vllm-omni-sligen1.*) rm -rf -- "$BUILD_DIR" ;;
    *) echo "Refusing to clean unexpected build directory: $BUILD_DIR" >&2 ;;
  esac
}
trap cleanup EXIT

SOURCE_VERSION="0.26.0"
TARGET_VERSION="${SOURCE_VERSION}+sligen1"
TARGET_DIST_INFO="vllm_omni-${TARGET_VERSION}.dist-info"

test -f "$OFFICIAL_WHEEL"
mkdir -p "$BUILD_DIR/unpacked" "$OUTPUT_DIR"
"$WHEEL_PYTHON" -m wheel unpack "$OFFICIAL_WHEEL" --dest "$BUILD_DIR/unpacked"
mv "$BUILD_DIR/unpacked/vllm_omni-${SOURCE_VERSION}" "$BUILD_DIR/stage"
mv "$BUILD_DIR/stage/vllm_omni-${SOURCE_VERSION}.dist-info" \
  "$BUILD_DIR/stage/$TARGET_DIST_INFO"
find "$BUILD_DIR/stage" -type d -name __pycache__ -prune -exec rm -rf -- {} +
find "$BUILD_DIR/stage" -type f -name '*.pyc' -delete
sed -i "s/^Version: ${SOURCE_VERSION}$/Version: ${TARGET_VERSION}/" \
  "$BUILD_DIR/stage/$TARGET_DIST_INFO/METADATA"
patch --batch --forward -d "$BUILD_DIR/stage" -p1 < "$PATCH_FILE"

"$WHEEL_PYTHON" -m compileall -q \
  "$BUILD_DIR/stage/vllm_omni/entrypoints/openai/serving_speech.py" \
  "$BUILD_DIR/stage/vllm_omni/model_executor/models/voxcpm2/voxcpm2_talker.py"
find "$BUILD_DIR/stage" -type d -name __pycache__ -prune -exec rm -rf -- {} +

"$WHEEL_PYTHON" -m wheel pack "$BUILD_DIR/stage" --dest-dir "$OUTPUT_DIR"
echo "Built $OUTPUT_DIR/vllm_omni-${TARGET_VERSION}-py3-none-any.whl"
