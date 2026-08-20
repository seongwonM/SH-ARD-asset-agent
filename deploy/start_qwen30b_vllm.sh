#!/usr/bin/env bash
set -euo pipefail

# Qwen3-30B-A3B-Instruct-2507-FP8 on RTX 3090 24GB x2.
# Requires: vllm>=0.8.5 in the active Python environment and a valid HF token if the model is gated.

MODEL="${MODEL:-Qwen/Qwen3-30B-A3B-Instruct-2507-FP8}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
TP="${TP:-2}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.90}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-8}"

exec vllm serve "${MODEL}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --tensor-parallel-size "${TP}" \
  --max-model-len "${MAX_MODEL_LEN}" \
  --gpu-memory-utilization "${GPU_MEM_UTIL}" \
  --max-num-seqs "${MAX_NUM_SEQS}" \
  --trust-remote-code
