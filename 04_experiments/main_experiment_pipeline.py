from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
import math
import random
import re
import sys
import time
import tokenize
import types
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


DEFAULT_BASE_DIR = Path("/mnt/data_4tb/ws/dataest test")
DEFAULT_DATASET = DEFAULT_BASE_DIR / "final_dataset" / "all_functions_final_2757.json"
DEFAULT_MAPPING = DEFAULT_BASE_DIR / "final_dataset" / "api_documentation_mapping.json"
DEFAULT_METADATA_DIR = DEFAULT_BASE_DIR / "开源文档" / "rapidapi_metadata" / "by_category"
DEFAULT_EXPERIMENT_DIR = DEFAULT_BASE_DIR / "实验"
DEFAULT_SEED = 20260607

CODEGEN_CONTEXTS = ("C0", "C1", "C2", "C3", "C4")
HEADER_EXCLUDE = {
    "x-rapidapi-host",
    "x-rapidapi-key",
    "x-rapidapi-proxy-secret",
    "content-type",
    "accept",
    "authorization",
    "user-agent",
}


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} is not valid JSONL") from exc
    return rows


def sample_id(index: int) -> str:
    return f"SCG-{index + 1:06d}"


def stable_id(*parts: str) -> str:
    raw = "::".join(parts).encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:16]


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_method(value: Any) -> str:
    method = normalize_text(value).upper()
    return method if method else "GET"


def normalize_route(value: Any) -> str:
    route = normalize_text(value)
    if not route:
        return ""
    if route.startswith("http://") or route.startswith("https://"):
        parsed = urlparse(route)
        route = parsed.path or "/"
    if not route.startswith("/"):
        route = "/" + route
    return route


def load_dataset(path: Path) -> list[dict[str, Any]]:
    data = load_json(path)
    if not isinstance(data, list):
        raise ValueError(f"Dataset must be a list: {path}")
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        row = dict(item)
        row["sample_id"] = row.get("sample_id") or sample_id(index)
        rows.append(row)
    return rows


def dataset_paths(base_dir: Path) -> dict[str, Path]:
    return {
        "dataset": base_dir / "final_dataset" / "all_functions_final_2757.json",
        "mapping": base_dir / "final_dataset" / "api_documentation_mapping.json",
        "metadata_dir": base_dir / "开源文档" / "rapidapi_metadata" / "by_category",
        "experiment_dir": base_dir / "实验",
    }


def get_api_metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("api_metadata")
    return metadata if isinstance(metadata, dict) else {}


def get_code(item: dict[str, Any]) -> str:
    code = item.get("code")
    if isinstance(code, dict):
        return normalize_text(code.get("complete_function"))
    return ""


def extract_docstring_summary(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node) or ""
            for line in doc.splitlines():
                line = line.strip()
                if line:
                    return line
    return ""


def words_from_name(name: str) -> str:
    text = re.sub(r"[_\-]+", " ", normalize_text(name))
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def make_task_hint(item: dict[str, Any]) -> str:
    metadata = get_api_metadata(item)
    docstring = extract_docstring_summary(get_code(item))
    pieces = [
        words_from_name(normalize_text(item.get("function_name"))),
        normalize_text(metadata.get("endpoint_name")),
        docstring,
    ]
    seen: set[str] = set()
    clean: list[str] = []
    for piece in pieces:
        key = piece.lower()
        if piece and key not in seen:
            clean.append(piece)
            seen.add(key)
    return " | ".join(clean)


def flatten_param_names(value: Any) -> set[str]:
    names: set[str] = set()
    if not value:
        return names
    if isinstance(value, dict):
        for key, subvalue in value.items():
            key_text = normalize_text(key)
            if key_text:
                names.add(key_text)
            if isinstance(subvalue, (dict, list)):
                names.update(flatten_param_names(subvalue))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                for key in ("name", "key", "param", "parameter", "field"):
                    name = normalize_text(item.get(key))
                    if name:
                        names.add(name)
                names.update(flatten_param_names(item.get("schema")))
            elif isinstance(item, str):
                text = item.strip()
                if re.match(r"^[A-Za-z_][A-Za-z0-9_.\-]*$", text):
                    names.add(text)
    elif isinstance(value, str):
        text = value.strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_.\-]*$", text):
            names.add(text)
    return {name for name in names if name.lower() not in HEADER_EXCLUDE}


def extract_dict_keys_from_text(text: str) -> set[str]:
    keys = set(re.findall(r"""["']([A-Za-z_][A-Za-z0-9_.\-]*)["']\s*:""", text))
    return {key for key in keys if key.lower() not in HEADER_EXCLUDE}


def extract_observed_param_names(metadata: dict[str, Any]) -> set[str]:
    code_params = metadata.get("code_params")
    if not isinstance(code_params, dict):
        return set()
    selected_fragments: list[str] = []
    for key, value in code_params.items():
        key_l = normalize_text(key).lower()
        if any(token in key_l for token in ("query", "params", "payload", "data", "json", "body")):
            selected_fragments.append(normalize_text(value))
    return set().union(*(extract_dict_keys_from_text(text) for text in selected_fragments)) if selected_fragments else set()


def extract_documented_param_names(metadata: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for field in ("params", "path_params", "header_params", "payload"):
        names.update(flatten_param_names(metadata.get(field)))
    return {name for name in names if name.lower() not in HEADER_EXCLUDE}


def make_ground_truth(item: dict[str, Any]) -> dict[str, Any]:
    metadata = get_api_metadata(item)
    doc_params = sorted(extract_documented_param_names(metadata))
    observed_params = sorted(extract_observed_param_names(metadata))
    headers = metadata.get("headers") if isinstance(metadata.get("headers"), dict) else {}
    return {
        "样本ID": item.get("sample_id"),
        "api_name": normalize_text(item.get("api_name")),
        "api_host": normalize_text(item.get("api_host")),
        "function_name": normalize_text(item.get("function_name")),
        "endpoint_id": normalize_text(metadata.get("endpoint_id")),
        "endpoint_name": normalize_text(metadata.get("endpoint_name")),
        "method": normalize_method(metadata.get("method")),
        "url": normalize_text(metadata.get("url")),
        "route": normalize_route(metadata.get("route")),
        "doc_match_type": normalize_text(metadata.get("doc_match_type")),
        "headers": headers,
        "documented_params": doc_params,
        "observed_params": observed_params,
        "param_ground_truth_source": "documentation" if doc_params else ("code_params" if observed_params else "none"),
        "task_hint": make_task_hint(item),
        "github": item.get("github_info", {}),
    }


def create_api_level_split(
    data: list[dict[str, Any]],
    seed: int = DEFAULT_SEED,
    train_ratio: float = 0.7,
    dev_ratio: float = 0.1,
) -> dict[str, list[str]]:
    apis = sorted({normalize_text(item.get("api_name")) for item in data if normalize_text(item.get("api_name"))})
    rng = random.Random(seed)
    rng.shuffle(apis)
    n = len(apis)
    train_n = int(round(n * train_ratio))
    dev_n = int(round(n * dev_ratio))
    train_apis = set(apis[:train_n])
    dev_apis = set(apis[train_n:train_n + dev_n])
    test_apis = set(apis[train_n + dev_n:])
    return {
        "train": sorted(train_apis),
        "dev": sorted(dev_apis),
        "test": sorted(test_apis),
    }


def split_dataset(args: argparse.Namespace) -> None:
    base_dir = Path(args.base_dir)
    paths = dataset_paths(base_dir)
    data = load_dataset(Path(args.dataset or paths["dataset"]))
    split = create_api_level_split(data, seed=args.seed)
    split_by_api = {api: name for name, apis in split.items() for api in apis}
    split_rows: dict[str, list[dict[str, Any]]] = {"train": [], "dev": [], "test": []}
    for item in data:
        split_name = split_by_api[normalize_text(item.get("api_name"))]
        row = dict(item)
        row["实验划分"] = split_name
        split_rows[split_name].append(row)

    out_dir = paths["experiment_dir"] / "数据划分"
    name_map = {"train": "训练集.json", "dev": "验证集.json", "test": "测试集.json"}
    for split_name, rows in split_rows.items():
        write_json(out_dir / name_map[split_name], rows)

    api_sets = {name: set(apis) for name, apis in split.items()}
    overlap = {
        "train_dev": sorted(api_sets["train"] & api_sets["dev"]),
        "train_test": sorted(api_sets["train"] & api_sets["test"]),
        "dev_test": sorted(api_sets["dev"] & api_sets["test"]),
    }
    stats = {
        "生成时间": current_time(),
        "随机种子": args.seed,
        "划分规则": "API-level split by api_name; the same API never appears in multiple splits.",
        "总样本数": len(data),
        "总API数": len(set(normalize_text(item.get("api_name")) for item in data)),
        "各划分样本数": {name: len(rows) for name, rows in split_rows.items()},
        "各划分API数": {name: len(apis) for name, apis in split.items()},
        "各划分doc_match_type": {
            name: dict(Counter(get_api_metadata(item).get("doc_match_type", "unknown") for item in rows))
            for name, rows in split_rows.items()
        },
        "API重叠检查": overlap,
        "输出文件": {name: str(out_dir / filename) for name, filename in name_map.items()},
    }
    write_json(out_dir / "划分统计.json", stats)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def current_time() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def render_param_list(names: list[str]) -> str:
    return ", ".join(names) if names else "None documented in the available metadata"


def render_endpoint_docs(gt: dict[str, Any], context: str) -> str:
    lines = []
    if context in {"C1", "C2", "C3", "C4"}:
        lines.append(f"API host: {gt['api_host']}")
    if context in {"C2", "C3", "C4"}:
        lines.extend([
            f"Endpoint URL: {gt['url']}",
            f"Route: {gt['route']}",
            f"HTTP method: {gt['method']}",
            f"Endpoint name: {gt['endpoint_name'] or 'N/A'}",
        ])
    if context in {"C3", "C4"}:
        params = gt["documented_params"] or gt["observed_params"]
        source = gt["param_ground_truth_source"]
        lines.extend([
            f"Headers: x-rapidapi-host must be {gt['api_host']}; use a placeholder x-rapidapi-key.",
            f"Parameters ({source}): {render_param_list(params)}",
        ])
    if context == "C4":
        lines.append("Dependency constraint: use the Python requests library or an equivalent direct HTTP client.")
    return "\n".join(lines)


def build_codegen_prompt(item: dict[str, Any], context: str) -> dict[str, Any]:
    gt = make_ground_truth(item)
    function_name = gt["function_name"] or "call_rapidapi_service"
    task_hint = gt["task_hint"] or words_from_name(function_name)
    system = (
        "You are evaluating API-grounded code generation. "
        "Generate only Python code, with no Markdown fences and no explanation."
    )
    user_lines = [
        "Task: Generate one standalone Python function for a RapidAPI service invocation.",
        f"Required function name: {function_name}",
        f"API name: {gt['api_name']}",
        f"Task hint extracted from the original GitHub function and endpoint metadata: {task_hint}",
        render_endpoint_docs(gt, context),
        "Requirements:",
        "- Do not call the API at generation time; only define the function.",
        "- The function should construct the request and return a parsed or raw response.",
        "- Use a placeholder API key parameter or read it from the environment; do not include any real secret.",
        "- Include x-rapidapi-host in the request headers when the host is known.",
    ]
    prompt = system + "\n\n" + "\n".join(line for line in user_lines if line)
    return {
        "样本ID": gt["样本ID"],
        "实验类型": "API-grounded代码生成",
        "上下文设置": context,
        "提示词": prompt,
        "ground_truth": gt,
    }


def load_split_rows(experiment_dir: Path, split_name: str) -> list[dict[str, Any]]:
    name_map = {"train": "训练集.json", "dev": "验证集.json", "test": "测试集.json"}
    path = experiment_dir / "数据划分" / name_map[split_name]
    if not path.exists():
        raise FileNotFoundError(f"Missing split file: {path}. Run split first.")
    return load_json(path)


def load_documented_endpoints(metadata_dir: Path) -> dict[str, list[dict[str, Any]]]:
    by_api: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not metadata_dir.exists():
        return by_api
    for path in sorted(metadata_dir.glob("*.json")):
        try:
            payload = load_json(path)
        except json.JSONDecodeError:
            continue
        apis = payload.get("apis") if isinstance(payload, dict) else None
        if not isinstance(apis, list):
            continue
        for api in apis:
            if not isinstance(api, dict):
                continue
            api_name = normalize_text(api.get("api_name") or api.get("api_slug"))
            api_host = normalize_text(api.get("rapidapi_host"))
            endpoints = api.get("endpoints_metadata")
            if not api_name or not isinstance(endpoints, list):
                continue
            for endpoint in endpoints:
                if not isinstance(endpoint, dict):
                    continue
                route = normalize_route(endpoint.get("route"))
                method = normalize_method(endpoint.get("method"))
                endpoint_id = normalize_text(endpoint.get("endpoint_id")) or stable_id(api_name, method, route)
                record = {
                    "endpoint_id": endpoint_id,
                    "endpoint_name": normalize_text(endpoint.get("endpoint_name")),
                    "method": method,
                    "route": route,
                    "url": normalize_text(endpoint.get("url")) or f"https://{api_host}{route}",
                    "api_host": normalize_text(endpoint.get("rapidapi_host")) or api_host,
                    "params": sorted(flatten_param_names(endpoint.get("params"))),
                    "path_params": sorted(flatten_param_names(endpoint.get("path_params"))),
                    "header_params": sorted(flatten_param_names(endpoint.get("header_params"))),
                    "payload": sorted(flatten_param_names(endpoint.get("payload"))),
                    "description": normalize_text(endpoint.get("description"))[:800],
                }
                by_api[api_name].append(record)
    for api_name, endpoints in list(by_api.items()):
        seen: set[tuple[str, str, str]] = set()
        unique: list[dict[str, Any]] = []
        for endpoint in endpoints:
            key = (endpoint["endpoint_id"], endpoint["method"], endpoint["route"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(endpoint)
        by_api[api_name] = unique
    return by_api


def make_dataset_endpoint_index(data: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_api: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in data:
        gt = make_ground_truth(item)
        by_api[gt["api_name"]].append({
            "endpoint_id": gt["endpoint_id"] or stable_id(gt["api_name"], gt["method"], gt["route"]),
            "endpoint_name": gt["endpoint_name"],
            "method": gt["method"],
            "route": gt["route"],
            "url": gt["url"],
            "api_host": gt["api_host"],
            "params": gt["documented_params"],
            "path_params": [],
            "header_params": [],
            "payload": [],
            "description": "",
        })
    for api_name, endpoints in list(by_api.items()):
        seen: set[tuple[str, str, str]] = set()
        unique: list[dict[str, Any]] = []
        for endpoint in endpoints:
            key = (endpoint["endpoint_id"], endpoint["method"], endpoint["route"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(endpoint)
        by_api[api_name] = unique
    return by_api


def choose_endpoint_candidates(
    item: dict[str, Any],
    doc_index: dict[str, list[dict[str, Any]]],
    dataset_index: dict[str, list[dict[str, Any]]],
    rng: random.Random,
    max_candidates: int = 10,
) -> list[dict[str, Any]]:
    gt = make_ground_truth(item)
    api_name = gt["api_name"]
    candidates = list(doc_index.get(api_name) or dataset_index.get(api_name) or [])
    target_key = (gt["endpoint_id"], gt["method"], gt["route"])

    def is_target(endpoint: dict[str, Any]) -> bool:
        return (
            normalize_text(endpoint.get("endpoint_id")) == gt["endpoint_id"]
            or (normalize_method(endpoint.get("method")) == gt["method"] and normalize_route(endpoint.get("route")) == gt["route"])
        )

    target = next((endpoint for endpoint in candidates if is_target(endpoint)), None)
    if target is None:
        target = {
            "endpoint_id": gt["endpoint_id"] or stable_id(api_name, gt["method"], gt["route"]),
            "endpoint_name": gt["endpoint_name"],
            "method": gt["method"],
            "route": gt["route"],
            "url": gt["url"],
            "api_host": gt["api_host"],
            "params": gt["documented_params"],
            "path_params": [],
            "header_params": [],
            "payload": [],
            "description": "",
        }
        candidates.append(target)

    distractors = [endpoint for endpoint in candidates if not is_target(endpoint)]
    rng.shuffle(distractors)
    selected = [target] + distractors[: max_candidates - 1]
    rng.shuffle(selected)
    for index, endpoint in enumerate(selected, start=1):
        endpoint["候选编号"] = f"E{index}"
    return selected


def build_endpoint_prompt(item: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    gt = make_ground_truth(item)
    candidate_lines = []
    for endpoint in candidates:
        param_names = sorted(set(endpoint.get("params", []) + endpoint.get("path_params", []) + endpoint.get("payload", [])))
        candidate_lines.append(
            f"{endpoint['候选编号']}. endpoint_id={endpoint['endpoint_id']} | method={endpoint['method']} | "
            f"route={endpoint['route']} | name={endpoint.get('endpoint_name') or 'N/A'} | params={render_param_list(param_names)}"
        )
    prompt = "\n".join([
        "Select the correct RapidAPI endpoint for the target Python function.",
        "Return only a JSON object with keys: endpoint_id, top3_endpoint_ids, method, route, parameters.",
        "The endpoint_id field is your Top-1 prediction. The top3_endpoint_ids field is an ordered list of up to three endpoint ids.",
        f"API name: {gt['api_name']}",
        f"API host: {gt['api_host']}",
        f"Function name: {gt['function_name']}",
        f"Task hint extracted from the original code and endpoint metadata: {gt['task_hint']}",
        "Candidate endpoints:",
        "\n".join(candidate_lines),
    ])
    return {
        "样本ID": gt["样本ID"],
        "实验类型": "Endpoint选择",
        "提示词": prompt,
        "候选数量": len(candidates),
        "candidates": candidates,
        "ground_truth": gt,
    }


def build_prompts(args: argparse.Namespace) -> None:
    base_dir = Path(args.base_dir)
    paths = dataset_paths(base_dir)
    experiment_dir = paths["experiment_dir"]
    split_rows = load_split_rows(experiment_dir, args.split)
    if args.limit:
        split_rows = split_rows[: args.limit]

    prompt_root = experiment_dir / "提示词"
    main_rows = [build_codegen_prompt(item, "C4") for item in split_rows]
    main_path = prompt_root / "API调用代码生成" / f"{args.split}_主实验_C4.jsonl"
    main_count = write_jsonl(main_path, main_rows)

    ablation_counts: dict[str, int] = {}
    for context in CODEGEN_CONTEXTS:
        rows = [build_codegen_prompt(item, context) for item in split_rows]
        path = prompt_root / "上下文消融" / f"{args.split}_{context}.jsonl"
        ablation_counts[context] = write_jsonl(path, rows)

    full_data = load_dataset(paths["dataset"])
    doc_index = load_documented_endpoints(paths["metadata_dir"])
    dataset_index = make_dataset_endpoint_index(full_data)
    rng = random.Random(args.seed)
    endpoint_rows: list[dict[str, Any]] = []
    skipped = Counter()
    for item in split_rows:
        gt = make_ground_truth(item)
        if gt["doc_match_type"] == "host_only":
            skipped["host_only_weak_alignment"] += 1
            continue
        candidates = choose_endpoint_candidates(item, doc_index, dataset_index, rng, max_candidates=args.max_candidates)
        if len(candidates) < 2:
            skipped["insufficient_candidates"] += 1
            continue
        endpoint_rows.append(build_endpoint_prompt(item, candidates))
    endpoint_path = prompt_root / "Endpoint选择" / f"{args.split}_endpoint选择.jsonl"
    endpoint_count = write_jsonl(endpoint_path, endpoint_rows)

    stats = {
        "生成时间": current_time(),
        "数据划分": args.split,
        "输入样本数": len(split_rows),
        "主实验提示词数": main_count,
        "上下文消融提示词数": ablation_counts,
        "Endpoint选择提示词数": endpoint_count,
        "Endpoint选择跳过原因": dict(skipped),
        "输出文件": {
            "主实验": str(main_path),
            "Endpoint选择": str(endpoint_path),
            "上下文消融目录": str(prompt_root / "上下文消融"),
        },
    }
    write_json(prompt_root / f"{args.split}_提示词生成统计.json", stats)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def extract_code_from_model_output(text: str) -> str:
    raw = normalize_text(text)
    if not raw:
        return ""
    fenced = re.findall(r"```(?:python|py)?\s*(.*?)```", raw, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        return max(fenced, key=len).strip()
    if "```" in raw:
        generic = re.findall(r"```\s*(.*?)```", raw, flags=re.DOTALL)
        if generic:
            return max(generic, key=len).strip()
    return raw


def ast_parse_ok(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def contains_host(code: str, host: str) -> bool:
    return bool(host) and host.lower() in code.lower()


def contains_endpoint(code: str, url: str, route: str) -> bool:
    code_l = code.lower()
    if url and url.lower() in code_l:
        return True
    if route and route.lower() in code_l:
        return True
    return False


def method_accuracy(code: str, method: str) -> bool:
    method_l = method.lower()
    patterns = [
        rf"\brequests\.{method_l}\s*\(",
        rf"\bhttpx\.{method_l}\s*\(",
        rf"""method\s*=\s*["']{method}["']""",
        rf"""["']{method}["']\s*,""",
    ]
    return any(re.search(pattern, code, flags=re.IGNORECASE) for pattern in patterns)


def header_accuracy(code: str, host: str) -> bool:
    code_l = code.lower()
    return "x-rapidapi-host" in code_l and (not host or host.lower() in code_l)


def response_handling(code: str) -> bool:
    return any(token in code for token in (".json()", ".text", ".content", "status_code", "raise_for_status()"))


def error_handling(code: str) -> bool:
    return bool(re.search(r"\btry\s*:", code) or re.search(r"\bexcept\b", code) or "raise_for_status()" in code or "status_code" in code)


def ast_dict_literal_keys(node: ast.AST) -> set[str]:
    if not isinstance(node, ast.Dict):
        return set()
    keys: set[str] = set()
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            if key.value.lower() not in HEADER_EXCLUDE:
                keys.add(key.value)
    return keys


def assigned_dicts(tree: ast.AST) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            keys = ast_dict_literal_keys(node.value)
            if not keys:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    mapping[target.id] = keys
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            keys = ast_dict_literal_keys(node.value) if node.value is not None else set()
            if keys:
                mapping[node.target.id] = keys
    return mapping


def extract_generated_param_names(code: str) -> set[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return extract_dict_keys_from_text(code)
    dict_assignments = assigned_dicts(tree)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = ""
        if isinstance(node.func, ast.Attribute):
            func_name = node.func.attr.lower()
        if func_name not in {"get", "post", "put", "delete", "patch", "request"}:
            continue
        for keyword in node.keywords:
            if keyword.arg not in {"params", "data", "json"}:
                continue
            if isinstance(keyword.value, ast.Dict):
                names.update(ast_dict_literal_keys(keyword.value))
            elif isinstance(keyword.value, ast.Name):
                names.update(dict_assignments.get(keyword.value.id, set()))
    if names:
        return {name for name in names if name.lower() not in HEADER_EXCLUDE}
    fallback = set()
    for variable, keys in dict_assignments.items():
        if any(token in variable.lower() for token in ("param", "query", "payload", "data", "body")):
            fallback.update(keys)
    return fallback or extract_dict_keys_from_text(code)


def precision_recall_f1(pred: set[str], gold: set[str]) -> dict[str, Any]:
    if not gold:
        return {
            "precision": None,
            "recall": None,
            "f1": None,
            "gold_count": 0,
            "pred_count": len(pred),
            "true_positive": 0,
        }
    tp = len({p.lower() for p in pred} & {g.lower() for g in gold})
    precision = tp / len(pred) if pred else 0.0
    recall = tp / len(gold) if gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "gold_count": len(gold),
        "pred_count": len(pred),
        "true_positive": tp,
    }


@dataclass
class FakeRequest:
    method: str
    url: str
    headers: dict[str, Any]
    params: Any
    json_body: Any
    data: Any


class FakeResponse:
    status_code = 200
    text = "{\"ok\": true}"
    content = b"{\"ok\": true}"

    def json(self) -> dict[str, Any]:
        return {"ok": True, "items": []}

    def raise_for_status(self) -> None:
        return None


class FakeRequests(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("requests")
        self.captured: list[FakeRequest] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.captured.append(FakeRequest(
            method=normalize_method(method),
            url=normalize_text(url),
            headers=kwargs.get("headers") or {},
            params=kwargs.get("params"),
            json_body=kwargs.get("json"),
            data=kwargs.get("data"),
        ))
        return FakeResponse()

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("DELETE", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("PATCH", url, **kwargs)


class FakeHttpx(FakeRequests):
    def __init__(self) -> None:
        super().__init__()
        self.__name__ = "httpx"


def safe_module_ast(tree: ast.Module) -> ast.Module:
    def is_safe_assignment_value(value: ast.AST | None) -> bool:
        if value is None:
            return True
        if isinstance(value, (ast.Constant, ast.Dict, ast.List, ast.Tuple, ast.Set)):
            return True
        if isinstance(value, ast.Name):
            return True
        if isinstance(value, ast.JoinedStr):
            return True
        return False

    allowed: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef)):
            allowed.append(node)
        elif isinstance(node, ast.Assign) and is_safe_assignment_value(node.value):
            allowed.append(node)
        elif isinstance(node, ast.AnnAssign) and is_safe_assignment_value(node.value):
            allowed.append(node)
        elif isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
            continue
    return ast.Module(body=allowed, type_ignores=[])


def dummy_value_for_arg(name: str) -> Any:
    lowered = name.lower()
    if "key" in lowered or "token" in lowered:
        return "DUMMY_RAPIDAPI_KEY"
    if "limit" in lowered or "count" in lowered or "page" in lowered or "offset" in lowered:
        return 1
    if "date" in lowered:
        return "2026-01-01"
    if "json" in lowered or lowered in {"input_data", "data", "payload"}:
        return "{\"query\": \"test\", \"id\": \"test\"}"
    if "id" in lowered:
        return "test_id"
    return "test"


def try_mock_execute(code: str, gt: dict[str, Any]) -> dict[str, Any]:
    if not ast_parse_ok(code):
        return {"mock_execution_ran": False, "mock_request_captured": False, "mock_pass": False, "error": "syntax_error"}
    try:
        tree = ast.parse(code)
        module_ast = safe_module_ast(tree)
        ast.fix_missing_locations(module_ast)
        fake_requests = FakeRequests()
        fake_httpx = FakeHttpx()
        old_requests = sys.modules.get("requests")
        old_httpx = sys.modules.get("httpx")
        sys.modules["requests"] = fake_requests
        sys.modules["httpx"] = fake_httpx
        namespace: dict[str, Any] = {
            "__name__": "mock_generated_module",
            "RAPIDAPI_KEY": "DUMMY_RAPIDAPI_KEY",
            "API_KEY": "DUMMY_RAPIDAPI_KEY",
        }
        exec(compile(module_ast, "<generated>", "exec"), namespace, namespace)
        functions = [obj for obj in namespace.values() if isinstance(obj, types.FunctionType)]
        if not functions:
            if old_requests is None:
                sys.modules.pop("requests", None)
            else:
                sys.modules["requests"] = old_requests
            if old_httpx is None:
                sys.modules.pop("httpx", None)
            else:
                sys.modules["httpx"] = old_httpx
            return {"mock_execution_ran": False, "mock_request_captured": False, "mock_pass": False, "error": "no_function"}
        target = next((fn for fn in functions if fn.__name__ == gt.get("function_name")), functions[0])
        args = []
        kwargs = {}
        sig = None
        try:
            import inspect
            sig = inspect.signature(target)
        except (TypeError, ValueError):
            pass
        if sig is not None:
            for param in sig.parameters.values():
                if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                    continue
                if param.default is param.empty:
                    args.append(dummy_value_for_arg(param.name))
        target(*args, **kwargs)
        if old_requests is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = old_requests
        if old_httpx is None:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = old_httpx
        captured = fake_requests.captured or fake_httpx.captured
        if not captured:
            return {"mock_execution_ran": True, "mock_request_captured": False, "mock_pass": False, "error": ""}
        request = captured[0]
        host_ok = gt["api_host"].lower() in str(request.headers).lower() or gt["api_host"].lower() in request.url.lower()
        endpoint_ok = contains_endpoint(request.url, gt["url"], gt["route"])
        method_ok = normalize_method(request.method) == normalize_method(gt["method"])
        return {
            "mock_execution_ran": True,
            "mock_request_captured": True,
            "mock_pass": bool(host_ok and endpoint_ok and method_ok),
            "captured_method": request.method,
            "captured_url": request.url,
            "captured_headers": request.headers,
            "error": "",
        }
    except Exception as exc:
        if "old_requests" in locals():
            if old_requests is None:
                sys.modules.pop("requests", None)
            else:
                sys.modules["requests"] = old_requests
        if "old_httpx" in locals():
            if old_httpx is None:
                sys.modules.pop("httpx", None)
            else:
                sys.modules["httpx"] = old_httpx
        return {
            "mock_execution_ran": False,
            "mock_request_captured": False,
            "mock_pass": False,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


def get_model_output_text(row: dict[str, Any]) -> str:
    for key in ("原始输出", "raw_output", "output", "response", "模型输出", "completion"):
        if key in row:
            return normalize_text(row.get(key))
    return ""


def get_prompt_id(row: dict[str, Any]) -> str:
    for key in ("样本ID", "sample_id", "id"):
        if key in row:
            return normalize_text(row.get(key))
    return ""


def evaluate_codegen_output(output_row: dict[str, Any], prompt_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    sample = get_prompt_id(output_row)
    prompt_row = prompt_by_id.get(sample)
    if not prompt_row:
        return {"样本ID": sample, "评估错误": "prompt_not_found"}
    gt = prompt_row["ground_truth"]
    raw_output = get_model_output_text(output_row)
    code = extract_code_from_model_output(raw_output)
    generated_params = extract_generated_param_names(code)
    doc_gold = set(gt.get("documented_params", []))
    observed_gold = set(gt.get("observed_params", []))
    doc_f1 = precision_recall_f1(generated_params, doc_gold)
    observed_f1 = precision_recall_f1(generated_params, observed_gold)
    mock = try_mock_execute(code, gt)
    metrics = {
        "样本ID": sample,
        "模型名称": output_row.get("模型名称") or output_row.get("model") or output_row.get("模型名") or "",
        "上下文设置": prompt_row.get("上下文设置", ""),
        "doc_match_type": gt.get("doc_match_type", ""),
        "解析后的代码": code,
        "Syntax Pass": ast_parse_ok(code),
        "Host Accuracy": contains_host(code, gt["api_host"]),
        "Endpoint Accuracy": contains_endpoint(code, gt["url"], gt["route"]),
        "Method Accuracy": method_accuracy(code, gt["method"]),
        "Header Accuracy": header_accuracy(code, gt["api_host"]),
        "Parameter F1 Documentation": doc_f1,
        "Parameter F1 Observed": observed_f1,
        "Parameter Ground Truth Source": gt.get("param_ground_truth_source"),
        "Generated Parameters": sorted(generated_params),
        "Response Handling Rate": response_handling(code),
        "Error Handling Rate": error_handling(code),
        "Mock Execution": mock,
        "Mock Execution Pass": mock.get("mock_pass", False),
    }
    return metrics


def parse_endpoint_answer(text: str) -> dict[str, Any]:
    raw = normalize_text(text)
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        raw = fenced[0].strip()
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    endpoint_id = ""
    method = ""
    route = ""
    endpoint_match = re.search(r"apiendpoint_[A-Za-z0-9_\-]+", raw)
    if endpoint_match:
        endpoint_id = endpoint_match.group(0)
    method_match = re.search(r"\b(GET|POST|PUT|DELETE|PATCH)\b", raw, flags=re.IGNORECASE)
    if method_match:
        method = method_match.group(1).upper()
    route_match = re.search(r"(/[A-Za-z0-9_./{}:\-]+)", raw)
    if route_match:
        route = route_match.group(1)
    params = re.findall(r"""["']([A-Za-z_][A-Za-z0-9_.\-]*)["']""", raw)
    return {"endpoint_id": endpoint_id, "method": method, "route": route, "parameters": params}


def endpoint_rank_metrics(answer: dict[str, Any], gt: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    pred_id = normalize_text(answer.get("endpoint_id"))
    pred_method = normalize_method(answer.get("method"))
    pred_route = normalize_route(answer.get("route"))
    gt_id = normalize_text(gt.get("endpoint_id"))
    gt_route = normalize_route(gt.get("route"))
    gt_method = normalize_method(gt.get("method"))

    raw_top3 = answer.get("top3_endpoint_ids") or answer.get("top3") or answer.get("top_3") or []
    if isinstance(raw_top3, str):
        top3_predictions = re.findall(r"apiendpoint_[A-Za-z0-9_\-]+", raw_top3)
    elif isinstance(raw_top3, list):
        top3_predictions = [normalize_text(item) for item in raw_top3]
    else:
        top3_predictions = []
    top1 = bool(pred_id and pred_id == gt_id) or bool(pred_route and pred_route == gt_route and pred_method == gt_method)
    top3 = top1 or bool(gt_id and gt_id in top3_predictions[:3])
    pred_params = set(flatten_param_names(answer.get("parameters")))
    gold_params = set(gt.get("documented_params") or gt.get("observed_params") or [])
    return {
        "Top-1 Endpoint Accuracy": top1,
        "Top-3 Endpoint Accuracy": bool(top3),
        "Route Accuracy": bool(pred_route and pred_route == gt_route),
        "Method Accuracy": bool(pred_method and pred_method == gt_method),
        "Parameter F1": precision_recall_f1(pred_params, gold_params),
        "parsed_answer": answer,
    }


def evaluate_endpoint_output(output_row: dict[str, Any], prompt_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    sample = get_prompt_id(output_row)
    prompt_row = prompt_by_id.get(sample)
    if not prompt_row:
        return {"样本ID": sample, "评估错误": "prompt_not_found"}
    answer = parse_endpoint_answer(get_model_output_text(output_row))
    gt = prompt_row["ground_truth"]
    metrics = endpoint_rank_metrics(answer, gt, prompt_row.get("candidates", []))
    return {
        "样本ID": sample,
        "模型名称": output_row.get("模型名称") or output_row.get("model") or "",
        "doc_match_type": gt.get("doc_match_type", ""),
        **metrics,
    }


def evaluate_outputs(args: argparse.Namespace) -> None:
    prompts = read_jsonl(Path(args.prompts))
    outputs = read_jsonl(Path(args.outputs))
    prompt_by_id = {normalize_text(row.get("样本ID")): row for row in prompts}
    evaluator = evaluate_endpoint_output if args.task == "endpoint" else evaluate_codegen_output
    results = [evaluator(row, prompt_by_id) for row in outputs]
    output_path = Path(args.output)
    write_json(output_path, results)
    summary = summarize_results(results, task=args.task)
    summary["生成时间"] = current_time()
    summary["提示词文件"] = str(Path(args.prompts))
    summary["模型输出文件"] = str(Path(args.outputs))
    summary["评估结果文件"] = str(output_path)
    write_json(output_path.with_name(output_path.stem + "_汇总.json"), summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def bool_mean(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [row.get(key) for row in rows if isinstance(row.get(key), bool)]
    if not vals:
        return None
    return sum(1 for value in vals if value) / len(vals)


def nested_f1_mean(rows: list[dict[str, Any]], key: str) -> float | None:
    vals: list[float] = []
    for row in rows:
        value = row.get(key)
        if isinstance(value, dict) and isinstance(value.get("f1"), (float, int)):
            vals.append(float(value["f1"]))
    if not vals:
        return None
    return sum(vals) / len(vals)


def summarize_results(rows: list[dict[str, Any]], task: str) -> dict[str, Any]:
    if task == "endpoint":
        metric_keys = [
            "Top-1 Endpoint Accuracy",
            "Top-3 Endpoint Accuracy",
            "Route Accuracy",
            "Method Accuracy",
        ]
        summary = {
            "任务": "Endpoint选择",
            "样本数": len(rows),
            **{key: bool_mean(rows, key) for key in metric_keys},
            "Parameter F1": nested_f1_mean(rows, "Parameter F1"),
            "按doc_match_type": {},
        }
    else:
        metric_keys = [
            "Syntax Pass",
            "Host Accuracy",
            "Endpoint Accuracy",
            "Method Accuracy",
            "Header Accuracy",
            "Response Handling Rate",
            "Error Handling Rate",
            "Mock Execution Pass",
        ]
        summary = {
            "任务": "API-grounded代码生成",
            "样本数": len(rows),
            **{key: bool_mean(rows, key) for key in metric_keys},
            "Parameter F1 Documentation": nested_f1_mean(rows, "Parameter F1 Documentation"),
            "Parameter F1 Observed": nested_f1_mean(rows, "Parameter F1 Observed"),
            "按doc_match_type": {},
        }
    for doc_type in sorted({row.get("doc_match_type", "unknown") for row in rows}):
        subset = [row for row in rows if row.get("doc_match_type", "unknown") == doc_type]
        summary["按doc_match_type"][doc_type] = summarize_results_without_groups(subset, task)
    return summary


def summarize_results_without_groups(rows: list[dict[str, Any]], task: str) -> dict[str, Any]:
    if task == "endpoint":
        return {
            "样本数": len(rows),
            "Top-1 Endpoint Accuracy": bool_mean(rows, "Top-1 Endpoint Accuracy"),
            "Top-3 Endpoint Accuracy": bool_mean(rows, "Top-3 Endpoint Accuracy"),
            "Route Accuracy": bool_mean(rows, "Route Accuracy"),
            "Method Accuracy": bool_mean(rows, "Method Accuracy"),
            "Parameter F1": nested_f1_mean(rows, "Parameter F1"),
        }
    return {
        "样本数": len(rows),
        "Syntax Pass": bool_mean(rows, "Syntax Pass"),
        "Host Accuracy": bool_mean(rows, "Host Accuracy"),
        "Endpoint Accuracy": bool_mean(rows, "Endpoint Accuracy"),
        "Method Accuracy": bool_mean(rows, "Method Accuracy"),
        "Header Accuracy": bool_mean(rows, "Header Accuracy"),
        "Parameter F1 Documentation": nested_f1_mean(rows, "Parameter F1 Documentation"),
        "Parameter F1 Observed": nested_f1_mean(rows, "Parameter F1 Observed"),
        "Response Handling Rate": bool_mean(rows, "Response Handling Rate"),
        "Error Handling Rate": bool_mean(rows, "Error Handling Rate"),
        "Mock Execution Pass": bool_mean(rows, "Mock Execution Pass"),
    }


def write_summary_tables(args: argparse.Namespace) -> None:
    result_dir = Path(args.result_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_files = sorted(result_dir.glob("*_汇总.json"))
    rows: list[dict[str, Any]] = []
    for path in summary_files:
        data = load_json(path)
        row = {
            "文件": path.name,
            "任务": data.get("任务"),
            "样本数": data.get("样本数"),
        }
        for key, value in data.items():
            if isinstance(value, (int, float)) or value is None:
                row[key] = value
        rows.append(row)
    if not rows:
        raise FileNotFoundError(f"No summary files found in {result_dir}")
    json_path = output_dir / "主实验汇总表.json"
    csv_path = output_dir / "主实验汇总表.csv"
    write_json(json_path, rows)
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"汇总文件数": len(rows), "json": str(json_path), "csv": str(csv_path)}, ensure_ascii=False, indent=2))


def inspect_prompts(args: argparse.Namespace) -> None:
    rows = read_jsonl(Path(args.prompts))
    print(json.dumps({
        "文件": args.prompts,
        "样本数": len(rows),
        "第一条样本": rows[0] if rows else None,
    }, ensure_ascii=False, indent=2)[: args.max_chars])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RapidAPI service invocation benchmark pipeline")
    parser.add_argument("--base-dir", default=str(DEFAULT_BASE_DIR), help="实验根目录，默认使用 /mnt/data_4tb/ws/dataest test")
    sub = parser.add_subparsers(dest="command", required=True)

    split = sub.add_parser("split", help="生成 API-level 训练/验证/测试划分")
    split.add_argument("--dataset", default="", help="可选：覆盖默认数据集路径")
    split.add_argument("--seed", type=int, default=DEFAULT_SEED)
    split.set_defaults(func=split_dataset)

    prompts = sub.add_parser("build-prompts", help="生成主实验、上下文消融和 endpoint 选择提示词")
    prompts.add_argument("--split", choices=["train", "dev", "test"], default="test")
    prompts.add_argument("--seed", type=int, default=DEFAULT_SEED)
    prompts.add_argument("--limit", type=int, default=0, help="调试时限制样本数；正式实验保持 0")
    prompts.add_argument("--max-candidates", type=int, default=10)
    prompts.set_defaults(func=build_prompts)

    evaluate = sub.add_parser("evaluate", help="评估模型输出 JSONL")
    evaluate.add_argument("--task", choices=["codegen", "endpoint"], default="codegen")
    evaluate.add_argument("--prompts", required=True)
    evaluate.add_argument("--outputs", required=True)
    evaluate.add_argument("--output", required=True)
    evaluate.set_defaults(func=evaluate_outputs)

    tables = sub.add_parser("make-tables", help="把评估汇总文件整理成结果表格草稿")
    tables.add_argument("--result-dir", required=True)
    tables.add_argument("--output-dir", required=True)
    tables.set_defaults(func=write_summary_tables)

    inspect = sub.add_parser("inspect-prompts", help="查看提示词文件首条样本")
    inspect.add_argument("--prompts", required=True)
    inspect.add_argument("--max-chars", type=int, default=8000)
    inspect.set_defaults(func=inspect_prompts)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
