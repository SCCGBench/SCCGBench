#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Dense retrieval baseline for SCCGBench (reproduces the dense rows of the
retrieval-quality table in the paper).

It builds an endpoint corpus from the released dataset (dedup by host+method+route),
encodes the corpus documents and the test-set intents with a neural sentence
encoder, ranks endpoints by cosine similarity, and reports Host@K / Endpoint@K
using the SAME hit criterion as the BM25 baseline (host match; host+method+route
match), so the sparse vs. dense comparison is strictly controlled.

Requirements: pip install sentence-transformers torch
Usage:
    python dense_retrieval_baseline.py --model sentence-transformers/all-MiniLM-L6-v2
    python dense_retrieval_baseline.py --model BAAI/bge-base-en-v1.5 \
        --query-prefix "Represent this sentence for searching relevant passages: "
"""
import argparse, json, re
from pathlib import Path
import numpy as np

DATASET = Path(__file__).resolve().parent.parent / "dataset" / "sccgbench_3135.json"
TESTSET = Path(__file__).resolve().parent.parent / "dataset" / "splits" / "test.json"


def norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip())


def norm_route(r):
    r = norm(r).split("?")[0]
    if r and not r.startswith("/"):
        r = "/" + r
    return r or "/"


def norm_method(m):
    return (norm(m).upper() or "GET")


def split_path_tokens(route):
    return " ".join(t for t in re.split(r"[/_\-.]", route) if t)


def param_names(meta):
    names = set()
    for k in ("params", "query_params", "path_params", "payload", "headers", "code_params"):
        v = meta.get(k)
        if isinstance(v, dict):
            names |= set(map(str, v.keys()))
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, dict) and x.get("name"):
                    names.add(str(x["name"]))
                elif isinstance(x, str):
                    names.add(x)
    return sorted(n for n in names if n)


def comments_text(item):
    c = item.get("comments")
    if isinstance(c, list):
        parts = []
        for e in c:
            t = e.get("text") if isinstance(e, dict) else e
            if isinstance(t, str) and t.strip():
                parts.append(t.strip())
        return "\n\n".join(parts)
    return norm(c)


def build_corpus(rows):
    seen, corpus = set(), []
    for it in rows:
        meta = it.get("api_metadata") if isinstance(it.get("api_metadata"), dict) else {}
        api_name = norm(it.get("api_name"))
        host = norm(it.get("api_host") or meta.get("api_host"))
        method = norm_method(meta.get("method"))
        route = norm_route(meta.get("route"))
        if not api_name or not host or not route:
            continue
        key = (host, method, route)
        if key in seen:
            continue
        seen.add(key)
        params = param_names(meta)
        search_text = " ".join([
            norm(it.get("api_name")), host, norm(meta.get("endpoint_name")),
            route, split_path_tokens(route), method, " ".join(params),
        ])
        corpus.append({"api_host": host, "method": method, "route": route, "search_text": search_text})
    return corpus


def ground_truth(item):
    meta = item.get("api_metadata") if isinstance(item.get("api_metadata"), dict) else {}
    return {"api_host": norm(item.get("api_host") or meta.get("api_host")),
            "method": norm_method(meta.get("method")), "route": norm_route(meta.get("route"))}


def hit(cands, gt, k, mode):
    for c in cands[:k]:
        if mode == "host" and c["api_host"] == gt["api_host"]:
            return True
        if mode == "endpoint" and c["api_host"] == gt["api_host"] and c["method"] == gt["method"] and c["route"] == gt["route"]:
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sentence-transformers/all-MiniLM-L6-v2")
    ap.add_argument("--query-prefix", default="")
    ap.add_argument("--device", default="cpu")
    args = ap.parse_args()
    from sentence_transformers import SentenceTransformer

    corpus = build_corpus(json.load(open(DATASET, encoding="utf-8")))
    rows = json.load(open(TESTSET, encoding="utf-8"))
    doc_texts = [d["search_text"] for d in corpus]
    queries = [args.query_prefix + comments_text(it) for it in rows]

    model = SentenceTransformer(args.model, device=args.device)
    d_emb = np.asarray(model.encode(doc_texts, batch_size=64, normalize_embeddings=True, show_progress_bar=False))
    q_emb = np.asarray(model.encode(queries, batch_size=64, normalize_embeddings=True, show_progress_bar=False))
    topk = np.argsort(-(q_emb @ d_emb.T), axis=1)[:, :10]

    N = len(rows)
    host = {k: 0 for k in (1, 3, 5, 10)}
    endp = {k: 0 for k in (1, 3, 5, 10)}
    for i, it in enumerate(rows):
        gt = ground_truth(it)
        cands = [corpus[j] for j in topk[i]]
        for k in (1, 3, 5, 10):
            host[k] += hit(cands, gt, k, "host")
            endp[k] += hit(cands, gt, k, "endpoint")
    res = {"model": args.model, "samples": N, "endpoint_corpus_size": len(corpus),
           "Host@K": {k: round(host[k] / N * 100, 2) for k in (1, 3, 5, 10)},
           "Endpoint@K": {k: round(endp[k] / N * 100, 2) for k in (1, 3, 5, 10)}}
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
