#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pass@k evaluation for SCCGBench (addresses the single-greedy-decoding threat).

It is a thin driver that REUSES the existing pipeline:
  1) generation : run_openai_compatible_model.py  (sample n times, temperature>0)
  2) evaluation : main_experiment_pipeline.py evaluate  (produces per-sample
                  "Mock Execution Pass")
For each test sample it counts how many of the n samples pass Mock, then computes
the unbiased estimator:  pass@k = E_samples[ 1 - C(n-c, k) / C(n, k) ].

Prerequisites:
  - First build the COMMENT_ONLY prompts:  python main_experiment_pipeline.py build-prompts --split test
  - A running OpenAI-compatible endpoint (e.g. Ollama: http://127.0.0.1:11434/v1).
Usage:
  python passk_evaluation.py --prompts <test_COMMENT_ONLY.jsonl> \
      --model qwen2.5-coder:7b --label qwen2.5-coder-7b --n 5 --temperature 0.8
Note: if your local `evaluate` sub-command takes different flags, adjust EVAL_CMD below.
"""
from __future__ import annotations
import argparse, json, math, subprocess, sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTDIR = HERE / "passk_runs"
BASE_URL = "http://127.0.0.1:11434/v1"   # local Ollama OpenAI-compatible endpoint


def generate(i, prompts, model, temp, top_p, limit, max_tokens):
    out = OUTDIR / f"gen_sample{i}.jsonl"
    if out.exists():
        out.unlink()
    cmd = [sys.executable, str(HERE / "run_openai_compatible_model.py"),
           "--prompts", str(prompts), "--output", str(out), "--model", model,
           "--base-url", BASE_URL, "--api-key-env", "",
           "--temperature", str(temp), "--top-p", str(top_p),
           "--max-tokens", str(max_tokens), "--timeout", "300"]
    if limit:
        cmd += ["--limit", str(limit)]
    subprocess.run(cmd, check=True)
    return out


def evaluate(i, prompts, gen_out):
    ev = OUTDIR / f"eval_sample{i}.json"
    cmd = [sys.executable, str(HERE / "main_experiment_pipeline.py"), "evaluate",
           "--task", "codegen", "--prompts", str(prompts),
           "--outputs", str(gen_out), "--output", str(ev)]
    subprocess.run(cmd, check=True)
    return ev


def per_sample_rows(ev):
    data = json.load(open(ev, encoding="utf-8"))
    return data if isinstance(data, list) else data.get("rows", [])


def passk_est(n, c, k):
    return 1.0 if n - c < k else 1.0 - math.comb(n - c, k) / math.comb(n, k)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompts", required=True, help="COMMENT_ONLY prompt JSONL (from build-prompts)")
    ap.add_argument("--model", default="qwen2.5-coder:7b")
    ap.add_argument("--label", default="qwen2.5-coder-7b")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--temperature", type=float, default=0.8)
    ap.add_argument("--top-p", type=float, default=0.95)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-tokens", type=int, default=1536)
    ap.add_argument("--ks", default="1,3,5")
    args = ap.parse_args()
    OUTDIR.mkdir(parents=True, exist_ok=True)
    ks = [int(x) for x in args.ks.split(",")]

    hits, seen = defaultdict(int), defaultdict(int)
    for i in range(args.n):
        gen_out = generate(i, args.prompts, args.model, args.temperature, args.top_p, args.limit, args.max_tokens)
        ev = evaluate(i, args.prompts, gen_out)
        for r in per_sample_rows(ev):
            sid = r.get("样本ID") or r.get("sample_id")
            if not sid:
                continue
            seen[sid] += 1
            hits[sid] += int(bool(r.get("Mock Execution Pass")))
        print(json.dumps({"round": i + 1, "covered": len(seen)}, ensure_ascii=False), flush=True)

    res = {"model": args.label, "n": args.n, "temperature": args.temperature, "samples": len(seen),
           "pass@k": {str(k): round(sum(passk_est(seen[s], hits[s], k) for s in seen if seen[s] >= k)
                                    / max(1, sum(seen[s] >= k for s in seen)) * 100, 2) for k in ks}}
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
