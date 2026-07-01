"""
Merge and quality-filter dataest_v3.1 results from three machines.

Run from /mnt/data_4tb/ws/dataes/dataest_v3.1 after copying Windows
machine2/machine3 crawled_data folders back into this directory.
"""

import hashlib
import json
import os
from urllib.parse import urlparse
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List, Set, Tuple


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "final_dataset")
RAPIDAPI_METADATA_DIR = os.path.join(BASE_DIR, "rapidapi_metadata", "by_category")

TARGET_FUNCTIONS = 12000
PREFERRED_MIN_SCORE = 70.0
FALLBACK_MIN_SCORE = 60.0
MAX_FUNCTIONS_PER_API = 30
MAX_FUNCTIONS_PER_REPO = 120

MACHINE_INPUTS = {
    "machine1": [
        os.path.join(BASE_DIR, "crawled_data", "all_functions_machine1.jsonl"),
        os.path.join(BASE_DIR, "crawled_data", "all_functions_machine1.json"),
    ],
    "machine2": [
        os.path.join(BASE_DIR, "machine2", "crawled_data", "all_functions_machine2.jsonl"),
        os.path.join(BASE_DIR, "machine2", "crawled_data", "all_functions_machine2.json"),
    ],
    "machine3": [
        os.path.join(BASE_DIR, "machine3", "crawled_data", "all_functions_machine3.jsonl"),
        os.path.join(BASE_DIR, "machine3", "crawled_data", "all_functions_machine3.json"),
    ],
}

GENERIC_PARAM_KEYS = {
    "name", "key", "type", "description", "required", "optional", "default",
    "example", "value", "values", "schema", "items", "properties", "format",
    "in", "param", "parameter", "parameters", "payload", "body", "headers",
}


def load_json_file(path: str) -> List[Dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def load_jsonl_file(path: str) -> List[Dict]:
    results = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                results.append(json.loads(line))
    return results


def load_first_available_machine_file(machine: str, paths: List[str]) -> Tuple[List[Dict], List[Dict]]:
    loaded_files = []

    for index, path in enumerate(paths):
        if not os.path.exists(path):
            loaded_files.append({"path": path, "status": "missing"})
            continue

        try:
            rows = load_jsonl_file(path) if path.endswith(".jsonl") else load_json_file(path)
        except Exception as exc:
            loaded_files.append({"path": path, "status": "error", "error": str(exc)})
            continue

        if not rows:
            loaded_files.append({"path": path, "status": "empty", "functions": 0})
            continue

        for row in rows:
            row["source_machine"] = machine
        loaded_files.append({"path": path, "status": "ok", "functions": len(rows)})

        for skipped_path in paths[index + 1:]:
            if os.path.exists(skipped_path):
                loaded_files.append({
                    "path": skipped_path,
                    "status": "skipped",
                    "reason": "higher_priority_source_loaded",
                })
        return rows, loaded_files

    return [], loaded_files


def load_machine_results() -> Tuple[List[Dict], Dict[str, Dict]]:
    all_results = []
    machine_stats = {}

    for machine, paths in MACHINE_INPUTS.items():
        machine_rows, loaded_files = load_first_available_machine_file(machine, paths)

        machine_stats[machine] = {
            "raw_functions": len(machine_rows),
            "loaded_files": loaded_files,
        }
        all_results.extend(machine_rows)

    return all_results, machine_stats


def get_checks(result: Dict) -> Dict:
    return result.get("quality_metrics", {}).get("completeness_checks", {})


def get_score(result: Dict) -> float:
    return float(result.get("quality_metrics", {}).get("code_quality_score", 0.0) or 0.0)


def get_code(result: Dict) -> str:
    return result.get("code", {}).get("complete_function", "")


def percent(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 2) if denominator else 0.0


def has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def load_rapidapi_doc_stats() -> Dict:
    api_names = set()
    endpoint_counts_by_api = Counter()
    endpoint_count = 0
    loaded_files = 0

    if not os.path.isdir(RAPIDAPI_METADATA_DIR):
        return {
            "total_documented_apis": 0,
            "total_documented_endpoints": 0,
            "loaded_metadata_files": 0,
            "endpoint_counts_by_api": endpoint_counts_by_api,
            "api_names": api_names,
        }

    for filename in sorted(os.listdir(RAPIDAPI_METADATA_DIR)):
        if not filename.endswith(".json"):
            continue

        path = os.path.join(RAPIDAPI_METADATA_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        loaded_files += 1
        apis = data.get("apis", []) if isinstance(data, dict) else data if isinstance(data, list) else []
        for api in apis:
            api_name = api.get("api_name")
            if api_name:
                api_names.add(api_name)
            api_endpoint_count = len(api.get("endpoints_metadata", []) or [])
            endpoint_count += api_endpoint_count
            if api_name:
                endpoint_counts_by_api[api_name] += api_endpoint_count

    return {
        "total_documented_apis": len(api_names),
        "total_documented_endpoints": endpoint_count,
        "loaded_metadata_files": loaded_files,
        "endpoint_counts_by_api": endpoint_counts_by_api,
        "api_names": api_names,
    }


def route_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).path or ""
    except Exception:
        return ""


def get_api_host(result: Dict, metadata: Dict) -> str:
    api_host = result.get("api_host", "")
    if api_host:
        return api_host

    headers = metadata.get("headers", {})
    if isinstance(headers, dict):
        return headers.get("x-rapidapi-host", "") or headers.get("X-RapidAPI-Host", "")

    return ""


def infer_doc_match_type(result: Dict) -> str:
    metadata = result.get("api_metadata", {}) or {}
    doc_match_type = metadata.get("doc_match_type")
    if doc_match_type in {"url", "route", "host_only", "none"}:
        return doc_match_type

    code = get_code(result)
    url = metadata.get("url", "")
    route = metadata.get("route", "") or route_from_url(url)
    api_host = get_api_host(result, metadata)

    if url and url in code:
        return "url"
    if route and route != "/" and route in code:
        return "route"
    if api_host and api_host in code:
        return "host_only"
    return "none"


def add_param_name(names: Set[str], value: Any):
    if not isinstance(value, str):
        return
    name = value.strip()
    if not name or len(name) > 80:
        return
    if name.lower() in GENERIC_PARAM_KEYS:
        return
    names.add(name)


def collect_param_names(value: Any) -> Set[str]:
    names = set()

    def visit(item: Any):
        if isinstance(item, dict):
            for key in ("name", "key", "param", "parameter", "field"):
                add_param_name(names, item.get(key))
            for key, child in item.items():
                if str(key).lower() not in GENERIC_PARAM_KEYS:
                    add_param_name(names, str(key))
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return names


def documented_param_values(metadata: Dict) -> List[Any]:
    values = []
    if metadata.get("params_source") == "documentation" and has_content(metadata.get("params")):
        values.append(metadata.get("params"))
    for key in ("path_params", "header_params", "payload"):
        value = metadata.get(key)
        if has_content(value):
            values.append(value)
    return values


def enrich_api_metadata(result: Dict) -> Dict:
    metadata = result.setdefault("api_metadata", {})

    if "code_params" not in metadata and metadata.get("params_source") != "documentation":
        metadata["code_params"] = metadata.get("params", {})
        metadata["params_source"] = "legacy_code_extraction"

    metadata.setdefault("route", route_from_url(metadata.get("url", "")))
    metadata.setdefault("endpoint_id", "")
    metadata.setdefault("endpoint_name", "")
    metadata.setdefault("path_params", None)
    metadata.setdefault("header_params", None)
    metadata.setdefault("payload", None)
    metadata.setdefault("doc_match_type", infer_doc_match_type(result))
    return result


def build_api_documentation_coverage(selected: List[Dict]) -> Dict:
    total = len(selected)
    doc_stats = load_rapidapi_doc_stats()
    documented_api_names = doc_stats["api_names"]
    endpoint_counts_by_api = doc_stats["endpoint_counts_by_api"]
    dataset_api_names = {result.get("api_name", "") for result in selected if result.get("api_name", "")}
    dataset_apis_with_docs = dataset_api_names & documented_api_names
    documented_endpoints_for_dataset_apis = sum(
        endpoint_counts_by_api.get(api_name, 0) for api_name in dataset_apis_with_docs
    )

    metadata_complete = 0
    endpoint_matched = 0
    host_only = 0
    no_doc_match = 0
    param_documented = 0
    param_used = 0
    matched_endpoint_keys = set()
    match_type_counts = Counter()

    for result in selected:
        metadata = result.get("api_metadata", {}) or {}
        code = get_code(result)
        headers = metadata.get("headers", {})

        if metadata.get("url") and metadata.get("method") and has_content(headers):
            metadata_complete += 1

        match_type = infer_doc_match_type(result)
        match_type_counts[match_type] += 1
        if match_type in {"url", "route"}:
            endpoint_matched += 1
            matched_endpoint_keys.add((
                result.get("api_name", ""),
                metadata.get("endpoint_id") or metadata.get("url") or metadata.get("route"),
            ))
        elif match_type == "host_only":
            host_only += 1
        else:
            no_doc_match += 1

        param_values = documented_param_values(metadata)
        if param_values:
            param_documented += 1
            param_names = set()
            for value in param_values:
                param_names.update(collect_param_names(value))
            if param_names and any(name in code for name in param_names):
                param_used += 1

    return {
        "metadata_complete_coverage": percent(metadata_complete, total),
        "endpoint_matched_coverage": percent(endpoint_matched, total),
        "host_only_coverage": percent(host_only, total),
        "param_documented_coverage": percent(param_documented, total),
        "param_used_coverage": percent(param_used, param_documented),
        "api_doc_to_dataset_coverage": percent(len(dataset_apis_with_docs), doc_stats["total_documented_apis"]),
        "dataset_endpoint_doc_coverage": percent(
            len(matched_endpoint_keys),
            documented_endpoints_for_dataset_apis,
        ),
        "counts": {
            "total_functions": total,
            "metadata_complete_functions": metadata_complete,
            "endpoint_matched_functions": endpoint_matched,
            "host_only_functions": host_only,
            "no_doc_match_functions": no_doc_match,
            "param_documented_functions": param_documented,
            "param_used_functions": param_used,
            "dataset_unique_apis": len(dataset_api_names),
            "dataset_apis_with_docs": len(dataset_apis_with_docs),
            "dataset_documented_endpoints": documented_endpoints_for_dataset_apis,
            "dataset_matched_endpoints": len(matched_endpoint_keys),
            "total_documented_apis": doc_stats["total_documented_apis"],
            "total_documented_endpoints": doc_stats["total_documented_endpoints"],
            "loaded_metadata_files": doc_stats["loaded_metadata_files"],
        },
        "match_types": dict(match_type_counts),
    }


def normalize_code(code: str) -> str:
    return "\n".join(line.rstrip() for line in code.strip().splitlines())


def dedupe_key(result: Dict) -> Tuple[str, str, str, str, str]:
    github_info = result.get("github_info", {})
    code_hash = hashlib.sha1(normalize_code(get_code(result)).encode("utf-8")).hexdigest()
    return (
        result.get("api_host", ""),
        github_info.get("repo", ""),
        github_info.get("file_path", ""),
        result.get("function_name", ""),
        code_hash,
    )


def is_quality_valid(result: Dict, min_score: float) -> Tuple[bool, str]:
    score = get_score(result)
    if score < min_score:
        return False, "score_below_threshold"

    checks = get_checks(result)
    if not checks.get("is_syntactically_valid", False):
        return False, "syntax_invalid"
    if not checks.get("has_api_call", False):
        return False, "missing_api_call"
    if checks.get("has_database_call", False):
        return False, "database_call"
    if checks.get("has_indentation_issue", False):
        return False, "indentation_issue"

    api_host = result.get("api_host", "")
    if api_host and api_host not in get_code(result):
        return False, "api_host_not_in_code"

    return True, "ok"


def rank_key(result: Dict) -> Tuple:
    code_info = result.get("code", {})
    github_info = result.get("github_info", {})
    match_priority = {"url": 3, "route": 2, "host_only": 1, "none": 0}
    return (
        match_priority.get(infer_doc_match_type(result), 0),
        get_score(result),
        bool(code_info.get("has_comments", False)),
        bool(code_info.get("has_docstring", False)),
        int(code_info.get("comment_lines", 0) or 0),
        int(github_info.get("stars", 0) or 0),
    )


def dedupe_results(results: Iterable[Dict]) -> Tuple[List[Dict], int]:
    seen = set()
    unique = []
    duplicate_count = 0

    for result in results:
        key = dedupe_key(result)
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        unique.append(result)

    return unique, duplicate_count


def filter_by_quality(results: Iterable[Dict], min_score: float) -> Tuple[List[Dict], Counter]:
    reject_reasons = Counter()
    kept = []

    for result in results:
        ok, reason = is_quality_valid(result, min_score)
        if ok:
            kept.append(result)
        else:
            reject_reasons[reason] += 1

    return kept, reject_reasons


def select_balanced(results: List[Dict], target: int) -> Tuple[List[Dict], Dict]:
    ranked = sorted(results, key=rank_key, reverse=True)
    selected = []
    api_counts = Counter()
    repo_counts = Counter()
    skipped_for_caps = 0

    for result in ranked:
        api_name = result.get("api_name", "")
        repo = result.get("github_info", {}).get("repo", "")

        if api_counts[api_name] >= MAX_FUNCTIONS_PER_API:
            skipped_for_caps += 1
            continue
        if repo_counts[repo] >= MAX_FUNCTIONS_PER_REPO:
            skipped_for_caps += 1
            continue

        selected.append(result)
        api_counts[api_name] += 1
        repo_counts[repo] += 1

        if len(selected) >= target:
            break

    if len(selected) < target:
        selected_keys = {dedupe_key(result) for result in selected}
        for result in ranked:
            if dedupe_key(result) in selected_keys:
                continue
            selected.append(result)
            selected_keys.add(dedupe_key(result))
            if len(selected) >= target:
                break

    return selected, {
        "skipped_for_diversity_caps": skipped_for_caps,
        "unique_apis": len(api_counts),
        "unique_repos": len(repo_counts),
    }


def build_report(
    raw_results: List[Dict],
    unique_results: List[Dict],
    quality_results: List[Dict],
    selected: List[Dict],
    machine_stats: Dict[str, Dict],
    duplicate_count: int,
    reject_reasons: Counter,
    min_score_used: float,
    selection_stats: Dict,
) -> Dict:
    scores = [get_score(result) for result in selected]
    api_counts = Counter(result.get("api_name", "") for result in selected)
    repo_counts = Counter(result.get("github_info", {}).get("repo", "") for result in selected)
    layer_counts = Counter(result.get("github_info", {}).get("query_layer", "") for result in selected)
    machine_counts = Counter(result.get("source_machine", "") for result in selected)
    comments = sum(1 for result in selected if result.get("code", {}).get("has_comments", False))
    docstrings = sum(1 for result in selected if result.get("code", {}).get("has_docstring", False))
    api_documentation_coverage = build_api_documentation_coverage(selected)

    return {
        "generated_at": datetime.now().isoformat(),
        "target_functions": TARGET_FUNCTIONS,
        "min_score_used": min_score_used,
        "raw_functions": len(raw_results),
        "unique_after_dedupe": len(unique_results),
        "duplicates_removed": duplicate_count,
        "quality_valid_functions": len(quality_results),
        "selected_functions": len(selected),
        "machine_inputs": machine_stats,
        "machine_distribution": dict(machine_counts),
        "quality": {
            "average": round(sum(scores) / len(scores), 2) if scores else 0,
            "min": round(min(scores), 2) if scores else 0,
            "max": round(max(scores), 2) if scores else 0,
            "score_90_plus": sum(1 for score in scores if score >= 90),
            "score_80_plus": sum(1 for score in scores if score >= 80),
            "score_70_plus": sum(1 for score in scores if score >= 70),
            "score_60_plus": sum(1 for score in scores if score >= 60),
        },
        "coverage": {
            "unique_apis": len(api_counts),
            "unique_repos": len(repo_counts),
            "max_functions_per_api": max(api_counts.values()) if api_counts else 0,
            "max_functions_per_repo": max(repo_counts.values()) if repo_counts else 0,
        },
        "documentation": {
            "comment_coverage": round(comments / len(selected) * 100, 2) if selected else 0,
            "docstring_coverage": round(docstrings / len(selected) * 100, 2) if selected else 0,
            "functions_with_comments": comments,
            "functions_with_docstrings": docstrings,
        },
        "api_documentation_coverage": api_documentation_coverage,
        "query_layers": dict(layer_counts),
        "reject_reasons": dict(reject_reasons),
        "selection": selection_stats,
    }


def build_api_documentation_mapping(selected: List[Dict], coverage: Dict) -> Dict:
    api_summary = {}
    mappings = []

    for result in selected:
        metadata = result.get("api_metadata", {}) or {}
        github_info = result.get("github_info", {}) or {}
        api_name = result.get("api_name", "")
        match_type = infer_doc_match_type(result)
        endpoint_key = metadata.get("endpoint_id") or metadata.get("url") or metadata.get("route")

        if api_name not in api_summary:
            api_summary[api_name] = {
                "api_name": api_name,
                "functions": 0,
                "unique_repos": set(),
                "matched_endpoints": set(),
                "match_types": Counter(),
            }

        summary = api_summary[api_name]
        summary["functions"] += 1
        if github_info.get("repo"):
            summary["unique_repos"].add(github_info.get("repo"))
        if match_type in {"url", "route"} and endpoint_key:
            summary["matched_endpoints"].add(endpoint_key)
        summary["match_types"][match_type] += 1

        mappings.append({
            "api_name": api_name,
            "api_host": result.get("api_host", ""),
            "function_name": result.get("function_name", ""),
            "repo": github_info.get("repo", ""),
            "file_path": github_info.get("file_path", ""),
            "query_layer": github_info.get("query_layer", ""),
            "doc_match_type": match_type,
            "endpoint_id": metadata.get("endpoint_id", ""),
            "endpoint_name": metadata.get("endpoint_name", ""),
            "method": metadata.get("method", ""),
            "url": metadata.get("url", ""),
            "route": metadata.get("route", ""),
            "params_source": metadata.get("params_source", ""),
            "code_params": metadata.get("code_params", {}),
        })

    api_summary_rows = []
    for summary in api_summary.values():
        api_summary_rows.append({
            "api_name": summary["api_name"],
            "functions": summary["functions"],
            "unique_repos": len(summary["unique_repos"]),
            "matched_endpoints": len(summary["matched_endpoints"]),
            "match_types": dict(summary["match_types"]),
        })

    api_summary_rows.sort(key=lambda row: (row["matched_endpoints"], row["functions"]), reverse=True)

    return {
        "generated_at": datetime.now().isoformat(),
        "summary": coverage,
        "api_summary": api_summary_rows,
        "mappings": mappings,
    }


def write_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    raw_results, machine_stats = load_machine_results()
    unique_results, duplicate_count = dedupe_results(raw_results)

    quality_results, reject_reasons = filter_by_quality(unique_results, PREFERRED_MIN_SCORE)
    min_score_used = PREFERRED_MIN_SCORE

    if len(quality_results) < TARGET_FUNCTIONS:
        quality_results, reject_reasons = filter_by_quality(unique_results, FALLBACK_MIN_SCORE)
        min_score_used = FALLBACK_MIN_SCORE

    selected, selection_stats = select_balanced(quality_results, TARGET_FUNCTIONS)
    selected = [enrich_api_metadata(result) for result in selected]
    report = build_report(
        raw_results=raw_results,
        unique_results=unique_results,
        quality_results=quality_results,
        selected=selected,
        machine_stats=machine_stats,
        duplicate_count=duplicate_count,
        reject_reasons=reject_reasons,
        min_score_used=min_score_used,
        selection_stats=selection_stats,
    )

    output_file = os.path.join(OUTPUT_DIR, "all_functions_final_12000.json")
    report_file = os.path.join(OUTPUT_DIR, "quality_report.json")
    mapping_file = os.path.join(OUTPUT_DIR, "api_documentation_mapping.json")
    mapping = build_api_documentation_mapping(selected, report["api_documentation_coverage"])
    write_json(output_file, selected)
    write_json(report_file, report)
    write_json(mapping_file, mapping)

    print("=" * 80)
    print("dataest_v3.1 final dataset")
    print("=" * 80)
    print(f"Raw functions: {len(raw_results)}")
    print(f"Unique functions: {len(unique_results)}")
    print(f"Quality-valid functions: {len(quality_results)} (min score {min_score_used})")
    print(f"Selected functions: {len(selected)} / target {TARGET_FUNCTIONS}")
    print(f"API doc endpoint matched: {report['api_documentation_coverage']['endpoint_matched_coverage']}%")
    print(f"API doc metadata complete: {report['api_documentation_coverage']['metadata_complete_coverage']}%")
    print(f"Dataset endpoint doc coverage: {report['api_documentation_coverage']['dataset_endpoint_doc_coverage']}%")
    print(f"Output: {output_file}")
    print(f"Report: {report_file}")
    print(f"Mapping: {mapping_file}")
    print("=" * 80)


if __name__ == "__main__":
    main()
