from __future__ import annotations

import ast
import io
import json
import re
import tokenize
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple


BASE_DIR = Path(__file__).resolve().parents[1]
INPUT_PATH = BASE_DIR / "final_dataset" / "all_functions_final_12000.json"
OUTPUT_DIR = BASE_DIR / "final_dataset" / "补充注释"
OUTPUT_PATH = OUTPUT_DIR / "补充注释数据集_2757.json"
ONLY_ANNOTATED_PATH = OUTPUT_DIR / "仅无注释样本_补充注释版.json"
REPORT_PATH = OUTPUT_DIR / "补充注释报告.json"


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def sample_id(index: int) -> str:
    return f"SCG-{index + 1:06d}"


def comment_tokens(code: str) -> List[str]:
    comments: List[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(code).readline):
            if tok.type == tokenize.COMMENT:
                comments.append(tok.string)
    except tokenize.TokenError:
        return comments
    return comments


def multiline_string_lines(code: str) -> set[int]:
    lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(code).readline):
            if tok.type == tokenize.STRING and tok.end[0] > tok.start[0]:
                lines.update(range(tok.start[0], tok.end[0] + 1))
    except tokenize.TokenError:
        return lines
    return lines


def has_function_docstring(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and ast.get_docstring(node):
            return True
    return False


def is_uncommented(code: str) -> bool:
    return not comment_tokens(code) and not has_function_docstring(code)


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def line_has_assignment(line: str, names: Tuple[str, ...]) -> bool:
    stripped = line.strip()
    return any(re.match(rf"^{re.escape(name)}\s*=", stripped) for name in names)


def classify_line(line: str) -> str | None:
    stripped = line.strip()
    lowered = stripped.lower()

    if not stripped:
        return None
    if stripped.startswith(("import ", "from ", "def ", "async def ", "return ", "else:", "elif ", "except ", "finally:")):
        return None

    if ".json()" in stripped or re.search(r"json\.loads\s*\(\s*response\.", stripped):
        return "Parse the JSON response returned by the API."
    if re.search(r"os\.environ|getenv|st\.secrets", lowered) or re.match(
        r"^(api_key|rapidapi_key|x_rapidapi_key|key)\s*=", lowered
    ):
        return "Load the API credential required for the RapidAPI request."
    if re.match(r"^(if|assert)\b", stripped) and any(token in lowered for token in ["not ", "missing", "required", "invalid", "none", "api_key", "key"]):
        return "Validate required inputs before constructing the API request."
    if line_has_assignment(line, ("url", "endpoint", "api_url", "base_url")) or re.match(r"^\w*url\w*\s*=", stripped):
        return "Define the target RapidAPI endpoint URL."
    if line_has_assignment(line, ("headers",)):
        return "Build the RapidAPI headers for the request."
    if line_has_assignment(line, ("params", "querystring", "query", "query_params")):
        return "Build the query parameters sent to the endpoint."
    if line_has_assignment(line, ("payload", "data", "json_data", "body")):
        return "Prepare the request payload for the API call."
    if line_has_assignment(line, ("files",)):
        return "Prepare file payloads for the API request."
    if re.search(r"\brequests\.(get|post|put|delete|patch|request)\s*\(", stripped) or re.search(r"\bhttpx\.(get|post|put|delete|patch|request)\s*\(", stripped):
        return "Send the HTTP request to the RapidAPI endpoint."
    if ".raise_for_status()" in stripped:
        return "Raise an exception if the API response reports an HTTP error."
    if "status_code" in stripped or re.search(r"\bresponse\.status\b", stripped):
        return "Check the API response status before parsing the payload."
    if any(token in stripped for token in [".text", ".content"]):
        return "Read the raw response content returned by the API."
    if re.match(r"^for\b", stripped) and any(token in lowered for token in ["page", "result", "item", "record", "data", "response"]):
        return "Iterate through API results and collect structured output."
    if re.match(r"^(try:|async with|with )", stripped):
        return "Wrap the API call and response handling in a controlled execution block."
    if any(token in lowered for token in ["append(", "extend(", "simplified", "result =", "results ="]):
        return "Normalize extracted API data into the function output format."
    return None


def should_insert_comment(previous_nonempty: str | None, comment: str) -> bool:
    if not previous_nonempty:
        return True
    previous = previous_nonempty.strip()
    return not previous.startswith("#") and comment not in previous


def annotate_code(code: str) -> Tuple[str, List[Dict[str, Any]]]:
    lines = code.splitlines()
    string_lines = multiline_string_lines(code)
    annotated: List[str] = []
    inserted: List[Dict[str, Any]] = []
    inserted_comments: set[str] = set()
    previous_nonempty: str | None = None

    for line_no, line in enumerate(lines, start=1):
        comment = None if line_no in string_lines else classify_line(line)
        if comment and comment not in inserted_comments and should_insert_comment(previous_nonempty, comment):
            indent = indent_of(line)
            annotated.append(f"{indent}# {comment}")
            inserted.append({
                "before_original_line": line_no,
                "comment": comment,
                "reason": "pattern_based_service_invocation_annotation",
            })
            inserted_comments.add(comment)

        annotated.append(line)
        if line.strip():
            previous_nonempty = line

    if not inserted:
        annotated, inserted = add_fallback_function_comment(lines)

    return "\n".join(annotated), inserted


def add_fallback_function_comment(lines: List[str]) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Add one safe function-body comment when no specific request pattern is found."""
    fallback = "Invoke the service API and return the processed result."
    for def_index, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped.startswith("def ") or stripped.startswith("async def ")):
            continue
        if not stripped.endswith(":"):
            continue
        def_indent = len(line) - len(line.lstrip())
        for body_index in range(def_index + 1, len(lines)):
            body = lines[body_index]
            if not body.strip():
                continue
            body_indent = len(body) - len(body.lstrip())
            if body_indent <= def_indent:
                break
            output = list(lines)
            output.insert(body_index, f"{body[:body_indent]}# {fallback}")
            return output, [{
                "before_original_line": body_index + 1,
                "comment": fallback,
                "reason": "fallback_function_level_annotation",
            }]
    return list(lines), []


def main() -> None:
    data = load_json(INPUT_PATH)
    enriched: List[Dict[str, Any]] = []
    annotated_only: List[Dict[str, Any]] = []
    match_counter = Counter()
    insertion_counter = Counter()

    for index, item in enumerate(data):
        new_item = dict(item)
        new_item["sample_id"] = sample_id(index)
        code = item.get("code", {}).get("complete_function", "")
        real_uncommented = is_uncommented(code)
        metadata = item.get("api_metadata", {}) or {}
        match_counter[metadata.get("doc_match_type", "unknown")] += 1

        annotation = {
            "sample_id": sample_id(index),
            "annotation_version": "v1_pattern_based_manual_assist",
            "source": "derived_from_original_code_without_overwriting",
            "original_had_inline_comment": bool(comment_tokens(code)),
            "original_had_function_docstring": has_function_docstring(code),
            "needs_supplemental_annotation": real_uncommented,
            "inserted_comment_count": 0,
            "inserted_comments": [],
            "notes": (
                "Comments are inserted only as explanatory annotations for request construction, "
                "API invocation, response parsing, and error handling. Original code is preserved."
            ),
        }

        code_info = dict(item.get("code", {}))
        code_info["original_function"] = code

        if real_uncommented:
            annotated_code, inserted = annotate_code(code)
            annotation["inserted_comment_count"] = len(inserted)
            annotation["inserted_comments"] = inserted
            code_info["annotated_function"] = annotated_code
            code_info["annotation_status"] = "supplemented"
            insertion_counter[len(inserted)] += 1
            annotated_only_item = dict(new_item)
            annotated_only_item["code"] = code_info
            annotated_only_item["annotation"] = annotation
            annotated_only.append(annotated_only_item)
        else:
            code_info["annotated_function"] = code
            code_info["annotation_status"] = "original_already_annotated_or_documented"

        new_item["code"] = code_info
        new_item["annotation"] = annotation
        enriched.append(new_item)

    report = {
        "input_path": str(INPUT_PATH),
        "output_path": str(OUTPUT_PATH),
        "only_annotated_path": str(ONLY_ANNOTATED_PATH),
        "total_samples": len(data),
        "supplemented_samples": len(annotated_only),
        "unchanged_samples": len(data) - len(annotated_only),
        "doc_match_distribution_all_samples": dict(match_counter),
        "inserted_comment_count_distribution": dict(sorted(insertion_counter.items())),
        "annotation_policy": {
            "overwrite_original": False,
            "original_code_field": "code.original_function",
            "annotated_code_field": "code.annotated_function",
            "selection_rule": "no tokenize COMMENT tokens and no function-level docstring in code.complete_function",
            "allowed_comment_focus": [
                "input validation",
                "API credential loading",
                "endpoint URL definition",
                "headers construction",
                "query/body/file payload construction",
                "HTTP request invocation",
                "response status checking",
                "JSON/raw response parsing",
                "result normalization",
                "error handling",
            ],
            "limitation": "Pattern-based annotations should be manually audited before being described as human-verified labels.",
        },
    }

    write_json(OUTPUT_PATH, enriched)
    write_json(ONLY_ANNOTATED_PATH, annotated_only)
    write_json(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
