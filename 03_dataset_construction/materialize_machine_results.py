"""Materialize each machine JSONL into JSON and statistics.

This is useful after an interrupted long crawl: JSONL is written continuously,
while the pretty JSON/statistics file is normally written at graceful exit.
"""

import json
import os
from collections import Counter
from datetime import datetime


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MACHINES = {
    "machine1": os.path.join(BASE_DIR, "crawled_data"),
    "machine2": os.path.join(BASE_DIR, "machine2", "crawled_data"),
    "machine3": os.path.join(BASE_DIR, "machine3", "crawled_data"),
}


def result_key(result):
    github_info = result.get("github_info", {})
    code = result.get("code", {})
    return (
        result.get("api_name", ""),
        github_info.get("repo", ""),
        github_info.get("file_path", ""),
        result.get("function_name", ""),
        code.get("complete_function", ""),
    )


def load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows

    seen = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            key = result_key(row)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def build_stats(rows):
    scores = [row.get("quality_metrics", {}).get("code_quality_score", 0) for row in rows]
    api_counts = Counter(row.get("api_name", "") for row in rows)
    repo_counts = Counter(row.get("github_info", {}).get("repo", "") for row in rows)
    layer_counts = Counter(row.get("github_info", {}).get("query_layer", "") for row in rows)
    comments = sum(1 for row in rows if row.get("code", {}).get("has_comments", False))
    docstrings = sum(1 for row in rows if row.get("code", {}).get("has_docstring", False))

    return {
        "generated_at": datetime.now().isoformat(),
        "total_functions": len(rows),
        "quality": {
            "average": round(sum(scores) / len(scores), 2) if scores else 0,
            "min": round(min(scores), 2) if scores else 0,
            "max": round(max(scores), 2) if scores else 0,
        },
        "coverage": {
            "unique_apis": len(api_counts),
            "unique_repos": len(repo_counts),
        },
        "documentation": {
            "comment_coverage": round(comments / len(rows) * 100, 2) if rows else 0,
            "docstring_coverage": round(docstrings / len(rows) * 100, 2) if rows else 0,
            "functions_with_comments": comments,
            "functions_with_docstrings": docstrings,
        },
        "query_layers": dict(layer_counts),
    }


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    print("=" * 80)
    print("materialize dataest_v3.1 machine results")
    print("=" * 80)
    for machine, crawled_dir in MACHINES.items():
        jsonl_path = os.path.join(crawled_dir, f"all_functions_{machine}.jsonl")
        json_path = os.path.join(crawled_dir, f"all_functions_{machine}.json")
        stats_path = os.path.join(crawled_dir, f"statistics_report_{machine}.json")

        rows = load_jsonl(jsonl_path)
        write_json(json_path, rows)
        write_json(stats_path, build_stats(rows))
        print(f"{machine}: {len(rows)} functions -> {json_path}")
    print("=" * 80)


if __name__ == "__main__":
    main()
