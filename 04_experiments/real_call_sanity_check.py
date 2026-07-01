#!/usr/bin/env python3
"""Controlled RapidAPI real-call sanity check for SCCGBench.

Safety properties:
- GET only; FREE/FREEMIUM APIs only; no side-effecting routes;
- credentials are read from RAPIDAPI_KEY and never serialized;
- response bodies and authorization headers are never logged;
- generated code is not executed: only requests already captured by the
  existing virtual executor and marked Mock=1 are replayed;
- dry-run is the default; --execute is required for network calls.
"""
from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[2]
SPLIT = ROOT / "实验/注释到代码/数据划分/测试集.json"
FULL_DATASET = ROOT / "final_dataset/all_functions_final_最终版_3135.json"
META = ROOT / "开源文档/rapidapi_metadata/by_category"
DEFAULT_OUT = ROOT / "实验/真实调用Sanity_Check"
UNSAFE = re.compile(r"(?:delete|remove|create|update|upload|send|pay|purchase|order|book|post|write|message|sms|email)", re.I)
PLACEHOLDER = re.compile(r"\{[^}]+\}|<[^>]+>")

def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

def pricing_by_host() -> dict[str, str]:
    out = {}
    for path in META.glob("*.json"):
        for api in load(path).get("apis", []):
            host = str(api.get("rapidapi_host") or "").lower()
            price = str(api.get("pricing") or "").upper()
            if host and price in {"FREE", "FREEMIUM"}: out[host] = price
    return out

def safe_value(name: str) -> str | int:
    n = name.lower()
    if any(x in n for x in ("limit", "count", "page", "offset")): return 1
    if "date" in n: return "2024-01-01"
    if "country" in n: return "US"
    if any(x in n for x in ("lat", "latitude")): return "40.7128"
    if any(x in n for x in ("lon", "lng", "longitude")): return "-74.0060"
    if "city" in n: return "New York"
    if "language" in n or n == "lang": return "en"
    if "id" in n: return "1"
    return "test"

def code_param_dict(code: str) -> dict[str, Any]:
    """Extract a request dictionary without executing repository code."""
    try: tree=ast.parse(code)
    except (SyntaxError,ValueError): return {}
    node=next((n.value for n in ast.walk(tree) if isinstance(n,(ast.Assign,ast.AnnAssign)) and isinstance(getattr(n,'value',None),ast.Dict)),None)
    if not isinstance(node,ast.Dict): return {}
    out={}
    for key,value in zip(node.keys,node.values):
        if not isinstance(key,ast.Constant) or not isinstance(key.value,str): continue
        name=key.value
        if any(token in name.lower() for token in ("rapidapi", "authorization", "token", "secret", "cookie")): continue
        if isinstance(value,ast.Constant) and isinstance(value.value,(str,int,float,bool)):
            raw=value.value
            # Do not replay credentials or long opaque literals from source.
            out[name]=safe_value(name) if isinstance(raw,str) and len(raw)>32 else raw
        else: out[name]=safe_value(name)
    return out

def fill_route_placeholders(route: str) -> str | None:
    if "{" in route and "}" not in route:
        return None
    def repl(match: re.Match[str]) -> str:
        return str(safe_value(match.group(1) or match.group(2)))
    return re.sub(r"\{([^}/?]+)\}|<([^>/]+)>", repl, route)

def gold_candidate(row: dict[str, Any], prices: dict[str, str], free_only: bool = False) -> dict[str, Any] | None:
    meta = row.get("api_metadata") or {}
    method = str(meta.get("method") or "GET").upper()
    host = str(row.get("api_host") or meta.get("api_host") or "").lower()
    route = str(meta.get("route") or "")
    name = " ".join(str(meta.get(k) or "") for k in ("endpoint_name", "description"))
    if method != "GET" or host not in prices or not route or UNSAFE.search(route + " " + name): return None
    if free_only and prices[host] != "FREE": return None
    route = fill_route_placeholders(route)
    if not route: return None
    raw_params = meta.get("code_params") or {}
    params: dict[str, Any] = {}
    if isinstance(raw_params,dict):
        for field,code in raw_params.items():
            if field in {"params","query_params"} and isinstance(code,list):
                params.update({str(n):safe_value(str(n)) for n in code})
            elif field != "headers_code" and isinstance(code,str):
                params.update(code_param_dict(code))
    url = f"https://{host}{route}"
    return {"sample_id": row.get("sample_id"), "api_name": row.get("api_name"), "pricing": prices[host],
            "method": "GET", "host": host, "url": url, "params": params}

def classify(status: int | None, error: str = "") -> str:
    if error: return "network_failure"
    if status == 429: return "rate_limit"
    if status in {401, 403}: return "authentication"
    if status is not None and status >= 500: return "service_unavailable"
    if status is not None and 200 <= status < 300: return "success_2xx"
    if status is not None and status == 402: return "quota_or_payment"
    return "reachable_non_2xx"

def call(url: str, host: str, key: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        r = requests.get(url, headers={"x-rapidapi-key": key, "x-rapidapi-host": host},
                         params=params, timeout=timeout, allow_redirects=False)
        return {"status_code": r.status_code, "outcome": classify(r.status_code),
                "elapsed_ms": round((time.monotonic()-started)*1000), "received_http_response": True}
    except requests.RequestException as exc:
        return {"status_code": None, "outcome": classify(None, type(exc).__name__),
                "elapsed_ms": round((time.monotonic()-started)*1000), "received_http_response": False,
                "error_type": type(exc).__name__}

def prepare(target: int, out: Path, source: Path = SPLIT, free_only: bool = False) -> list[dict[str, Any]]:
    prices = pricing_by_host(); seen = set(); rows = []
    for item in load(source):
        cand = gold_candidate(item, prices, free_only=free_only)
        if cand and cand["host"] not in seen:
            seen.add(cand["host"]); rows.append(cand)
    # Prefer genuinely FREE services. FREEMIUM services are retained only as a
    # fallback because their free-tier availability can depend on subscription
    # state and remaining quota.
    rows.sort(key=lambda x: (0 if x["pricing"] == "FREE" else 1, x["host"]))
    manifest = rows[:max(target * 4, 120)]
    dump(out / "candidate_manifest.json", manifest)
    return manifest

def probe_gold(candidates: list[dict[str, Any]], key: str, target: int, timeout: float, delay: float, execute: bool, out: Path) -> list[dict[str, Any]]:
    if not execute:
        dump(out / "gold_probe_dry_run.json", {"candidate_count": len(candidates), "network_calls": 0})
        return []
    results=[]; selected=[]
    for c in candidates:
        result = call(c["url"], c["host"], key, c["params"], timeout)
        record = {k:v for k,v in c.items() if k != "params"} | result
        results.append(record)
        if result["outcome"] == "success_2xx": selected.append({k:v for k,v in c.items() if k != "params"})
        if len(selected) >= target: break
        time.sleep(delay)
    dump(out / "gold_probe_results.json", results); dump(out / "selected_gold_reachable.json", selected)
    return selected

def sanitized_generated_url(url: str) -> tuple[str, dict[str, str]]:
    parts=urlsplit(url); params=dict(parse_qsl(parts.query, keep_blank_values=True))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")), params

def probe_generated(selected: list[dict[str, Any]], key: str, timeout: float, delay: float, execute: bool, out: Path) -> None:
    selected_ids={x["sample_id"] for x in selected}; selected_hosts={x["host"] for x in selected}
    records=[]
    pattern = str(ROOT / "实验/注释到代码/自动评估结果/*/新标准_test_*_评估.json")
    for path_s in sorted(glob.glob(pattern)):
        path=Path(path_s); model=path.parent.name
        for row in load(path):
            if row.get("样本ID") not in selected_ids or not row.get("Mock Execution Pass"): continue
            req=row.get("Mock Execution") or {}; url=str(req.get("captured_url") or "")
            parts=urlsplit(url); host=parts.hostname or ""
            if req.get("captured_method") != "GET" or host not in selected_hosts: continue
            clean_url, params=sanitized_generated_url(url)
            base={"model":model,"setting":row.get("上下文设置"),"sample_id":row.get("样本ID"),"mock":1,
                  "method":"GET","host":host,"url":clean_url}
            if execute:
                base.update(call(clean_url,host,key,params,timeout)); time.sleep(delay)
            else: base.update({"outcome":"dry_run","network_calls":0})
            records.append(base)
    dump(out / ("generated_probe_results.json" if execute else "generated_probe_dry_run.json"), records)
    if not execute: return
    external={"network_failure","rate_limit","quota_or_payment","authentication","service_unavailable"}
    n=len(records); reachable=sum(r["received_http_response"] for r in records); ok=sum(r["outcome"]=="success_2xx" for r in records)
    summary={"mock_1_requests_tested":n,"received_http_response":reachable,
             "received_http_response_rate":reachable/n if n else None,"success_2xx":ok,
             "success_2xx_rate":ok/n if n else None,"external_failures":sum(r["outcome"] in external for r in records),
             "outcomes":dict(Counter(r["outcome"] for r in records)),
             "scope":"Small-scale sanity check; does not replace the virtual-execution main evaluation."}
    dump(out / "real_call_sanity_summary.json",summary)

def main() -> None:
    ap=argparse.ArgumentParser(); ap.add_argument("stage",choices=["prepare","probe-gold","probe-generated","all"])
    ap.add_argument("--target",type=int,default=40); ap.add_argument("--timeout",type=float,default=15)
    ap.add_argument("--delay",type=float,default=1.0); ap.add_argument("--execute",action="store_true")
    ap.add_argument("--output-dir",type=Path,default=DEFAULT_OUT)
    ap.add_argument("--source",choices=["test","full"],default="test")
    ap.add_argument("--free-only",action="store_true")
    ap.add_argument("--key-stdin",action="store_true",help="通过无回显终端读取密钥；不写入参数、日志或文件")
    args=ap.parse_args()
    if not 30 <= args.target <= 50: raise SystemExit("--target must be between 30 and 50")
    key=os.environ.get("RAPIDAPI_KEY","")
    if args.execute and not key and args.key_stdin:
        # Intended for a pipe controlled by the caller. No prompt and no echo;
        # the value remains in memory only for this process.
        key=sys.stdin.readline().strip()
    if args.execute and not key: raise SystemExit("RAPIDAPI_KEY is required with --execute")
    source = FULL_DATASET if args.source == "full" else SPLIT
    candidates=prepare(args.target,args.output_dir,source,free_only=args.free_only)
    selected=load(args.output_dir/"selected_gold_reachable.json") if (args.output_dir/"selected_gold_reachable.json").exists() else []
    if args.stage in {"probe-gold","all"}: selected=probe_gold(candidates,key,args.target,args.timeout,args.delay,args.execute,args.output_dir)
    if args.stage in {"probe-generated","all"}: probe_generated(selected,key,args.timeout,args.delay,args.execute,args.output_dir)
    if args.stage == "prepare": print(json.dumps({"candidates":len(candidates),"network_calls":0},ensure_ascii=False))

if __name__ == "__main__": main()
