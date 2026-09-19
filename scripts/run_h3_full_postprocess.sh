#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/donxu/ai-centre"
RUN_DIR="${ROOT}/runtime/validation/h3-full-benchmark-20260902"
QUALITY_LIBS="${ROOT}/runtime/validation/h3-quality-libs"
QUALITY_MODELS="${ROOT}/runtime/validation/h3-quality-models"
REPORT_DIR="${ROOT}/docs/reports"

while [[ ! -f "${RUN_DIR}/results.json" ]]; do
  if ! systemctl --user is-active --quiet ai-centre-h3-benchmark-20260902.service; then
    echo "generation service stopped before results.json was produced" >&2
    exit 1
  fi
  sleep 60
done

cd "${ROOT}"
export PYTHONPATH="${ROOT}"
"${ROOT}/.venv-depth/bin/python" "${ROOT}/scripts/analyze_h3_full_matrix.py" \
  "${RUN_DIR}" media

export PYTHONPATH="${QUALITY_LIBS}:${ROOT}"
export HF_HOME="${QUALITY_MODELS}"
export HF_ENDPOINT="https://hf-mirror.com"
"${ROOT}/.venv-depth/bin/python" "${ROOT}/scripts/analyze_h3_full_matrix.py" \
  "${RUN_DIR}" quality --device cpu
"${ROOT}/.venv-depth/bin/python" "${ROOT}/scripts/analyze_h3_full_matrix.py" \
  "${RUN_DIR}" report

mkdir -p "${REPORT_DIR}"
cp "${RUN_DIR}/report.md" \
  "${REPORT_DIR}/MINIMAX_H3_FULL_BENCHMARK_20260902.md"
cp "${RUN_DIR}/review.html" \
  "${REPORT_DIR}/MINIMAX_H3_FULL_BENCHMARK_20260902.html"

echo "H3 benchmark post-processing completed"
