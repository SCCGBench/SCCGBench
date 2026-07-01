"""Validate the final dataest_v3.1 dataset against the 12000-quality goal."""

import hashlib
import json
import os
import sys


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_PATH = os.path.join(BASE_DIR, "final_dataset", "all_functions_final_12000.json")
REPORT_PATH = os.path.join(BASE_DIR, "final_dataset", "quality_report.json")

MIN_COUNT = 11000
MAX_COUNT = 12500
MIN_SCORE = 60.0
MIN_AVG_SCORE = 80.0
MIN_COMMENT_COVERAGE = 60.0


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def code_hash(code):
    normalized = "\n".join(line.rstrip() for line in code.strip().splitlines())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def validate():
    failures = []

    if not os.path.exists(DATASET_PATH):
        failures.append(f"missing dataset: {DATASET_PATH}")
        return failures
    if not os.path.exists(REPORT_PATH):
        failures.append(f"missing report: {REPORT_PATH}")
        return failures

    data = load_json(DATASET_PATH)
    report = load_json(REPORT_PATH)

    count = len(data)
    if not (MIN_COUNT <= count <= MAX_COUNT):
        failures.append(f"dataset count {count} not in [{MIN_COUNT}, {MAX_COUNT}]")

    seen = set()
    duplicate_count = 0
    scores = []
    comment_count = 0

    for idx, item in enumerate(data):
        code_info = item.get("code", {})
        github_info = item.get("github_info", {})
        metrics = item.get("quality_metrics", {})
        checks = metrics.get("completeness_checks", {})
        code = code_info.get("complete_function", "")
        api_host = item.get("api_host", "")
        score = float(metrics.get("code_quality_score", 0) or 0)
        scores.append(score)

        key = (
            item.get("api_host", ""),
            github_info.get("repo", ""),
            github_info.get("file_path", ""),
            item.get("function_name", ""),
            code_hash(code),
        )
        if key in seen:
            duplicate_count += 1
        seen.add(key)

        if score < MIN_SCORE:
            failures.append(f"item {idx} score below {MIN_SCORE}: {score}")
        if not checks.get("is_syntactically_valid", False):
            failures.append(f"item {idx} syntax invalid")
        if not checks.get("has_api_call", False):
            failures.append(f"item {idx} missing API call")
        if checks.get("has_database_call", False):
            failures.append(f"item {idx} has database call")
        if checks.get("has_indentation_issue", False):
            failures.append(f"item {idx} has indentation issue")
        if api_host and api_host not in code:
            failures.append(f"item {idx} missing api_host in code")

        if code_info.get("has_comments", False):
            comment_count += 1

    if duplicate_count:
        failures.append(f"duplicate functions: {duplicate_count}")

    avg_score = sum(scores) / len(scores) if scores else 0
    if avg_score < MIN_AVG_SCORE:
        failures.append(f"average score {avg_score:.2f} below {MIN_AVG_SCORE}")

    comment_coverage = comment_count / count * 100 if count else 0
    if comment_coverage < MIN_COMMENT_COVERAGE:
        failures.append(f"comment coverage {comment_coverage:.2f}% below {MIN_COMMENT_COVERAGE}%")

    report_count = report.get("selected_functions")
    if report_count != count:
        failures.append(f"report selected_functions {report_count} != dataset count {count}")

    api_doc_coverage = report.get("api_documentation_coverage")
    if not isinstance(api_doc_coverage, dict):
        failures.append("report missing api_documentation_coverage")
        api_doc_coverage = {}

    print("=" * 80)
    print("dataest_v3.1 final dataset validation")
    print("=" * 80)
    print(f"dataset: {DATASET_PATH}")
    print(f"count: {count}")
    print(f"average score: {avg_score:.2f}")
    print(f"comment coverage: {comment_coverage:.2f}%")
    print(f"duplicates: {duplicate_count}")
    print(f"report min_score_used: {report.get('min_score_used')}")
    if api_doc_coverage:
        print(f"API doc metadata complete: {api_doc_coverage.get('metadata_complete_coverage', 0)}%")
        print(f"API doc endpoint matched: {api_doc_coverage.get('endpoint_matched_coverage', 0)}%")
        print(f"API doc host only: {api_doc_coverage.get('host_only_coverage', 0)}%")
        print(f"API doc params documented: {api_doc_coverage.get('param_documented_coverage', 0)}%")
        print(f"API doc params used: {api_doc_coverage.get('param_used_coverage', 0)}%")
        print(f"API doc to dataset coverage: {api_doc_coverage.get('api_doc_to_dataset_coverage', 0)}%")
        print(f"Dataset endpoint doc coverage: {api_doc_coverage.get('dataset_endpoint_doc_coverage', 0)}%")
    print("=" * 80)

    return failures


def main():
    failures = validate()
    if failures:
        print("FAILED")
        for failure in failures[:50]:
            print(f"- {failure}")
        if len(failures) > 50:
            print(f"... {len(failures) - 50} more failures")
        sys.exit(1)

    print("PASSED")


if __name__ == "__main__":
    main()
