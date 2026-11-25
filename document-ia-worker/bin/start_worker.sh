#!/usr/bin/env bash
set -euo pipefail

# Prevent glibc memory fragmentation in Scalingo Linux containers
export MALLOC_ARENA_MAX=${MALLOC_ARENA_MAX:-2}

# Limit CPU thread pools for PyTorch, OpenCV, OpenMP to prevent memory inflation
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-1}
export MKL_NUM_THREADS=${MKL_NUM_THREADS:-1}
export OPENBLAS_NUM_THREADS=${OPENBLAS_NUM_THREADS:-1}
export OPENCV_FOR_THREADS_NUM=${OPENCV_FOR_THREADS_NUM:-1}

# Enforce offline Ultralytics execution and writable config directory
export YOLO_OFFLINE="${YOLO_OFFLINE:-true}"
export YOLO_CONFIG_DIR="${YOLO_CONFIG_DIR:-/tmp/Ultralytics}"

# The models downloaded and verified during build are bundled in .models/
APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS_DIR="${MODELS_DIR:-$APP_ROOT/.models}"

export YOLOWORLD_PATH="${YOLOWORLD_PATH:-$MODELS_DIR/yolov8m-world.pt}"
export CLIP_WEIGHTS_PATH="${CLIP_WEIGHTS_PATH:-$MODELS_DIR/clip/ViT-B-32.pt}"
export QRDET_WEIGHTS_DIR="${QRDET_WEIGHTS_DIR:-$MODELS_DIR/qrdet}"
export TESSDATA_PREFIX="${TESSDATA_PREFIX:-$MODELS_DIR/tessdata}"

echo "[boot] Verifying SHA-256 integrity of bundled model artifacts in $MODELS_DIR..."
PYTHONPATH="$APP_ROOT/src:${PYTHONPATH:-}" python -m document_ia_worker.core.model_artifacts \
  --verify \
  --models-dir "$MODELS_DIR"

echo "[boot] Model artifacts verified (YOLOWORLD_PATH=$YOLOWORLD_PATH, CLIP_WEIGHTS_PATH=$CLIP_WEIGHTS_PATH, QRDET_WEIGHTS_DIR=$QRDET_WEIGHTS_DIR, TESSDATA_PREFIX=$TESSDATA_PREFIX)"

exec python -u src/document_ia_worker/main.py
