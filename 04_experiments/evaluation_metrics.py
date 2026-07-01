from __future__ import annotations

import argparse
import ast
import csv
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any


def load_pipeline() -> Any:
    path = Path(__file__).resolve().with_name("main_experiment_pipeline.py")
    spec = importlib.util.spec_from_file_location("main_experiment_pipeline", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import pipeline script: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PIPELINE = load_pipeline()


HTTP_CLIENTS = ("requests", "httpx", "urllib", "aiohttp")
PLACEHOLDER_TOKENS = (
    "placeholder",
    "your_",
    "your-",
    "dummy",
    "test",
    "api_key",
    "rapidapi_key",
    "rapidapi-key",
    "key_here",
    "insert",
    "replace",
)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return PIPELINE.read_jsonl(path)


def parse_tree(code: str) -> ast.Module | None:
    try:
        return ast.parse(code)
    except SyntaxError:
        return None


def function_names(code: str) -> set[str]:
    tree = parse_tree(code)
    if tree is None:
        return set()
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def function_name_accuracy(code: str, expected: str) -> bool:
    return bool(expected) and expected in function_names(code)


def dependency_info(code: str) -> dict[str, Any]:
    tree = parse_tree(code)
    imported: set[str] = set()
    used: set[str] = set()
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in HTTP_CLIENTS:
                        imported.add(root)
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in HTTP_CLIENTS:
                    imported.add(root)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id in HTTP_CLIENTS:
                    used.add(node.value.id)
    lowered = code.lower()
    for client in HTTP_CLIENTS:
        if re.search(rf"\b{client}\.", lowered):
            used.add(client)
    clients = sorted(imported | used)
    return {
        "Dependency Accuracy": bool(clients),
        "HTTP Clients": clients,
        "Imported HTTP Clients": sorted(imported),
        "Used HTTP Clients": sorted(used),
    }


def return_value_handling(code: str) -> bool:
    tree = parse_tree(code)
    if tree is None:
        return False
    has_return = any(isinstance(node, ast.Return) for node in ast.walk(tree))
    response_token = any(token in code for token in (".json()", ".text", ".content", "return response"))
    return bool(has_return and response_token)


def suspicious_secret_literals(code: str) -> list[str]:
    findings: set[str] = set()
    for match in re.finditer(r"\b[A-Za-z0-9]{8,}msh[A-Za-z0-9]{8,}\b", code):
        findings.add(match.group(0))

    tree = parse_tree(code)
    if tree is None:
        return sorted(findings)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if key.value.lower() != "x-rapidapi-key":
                continue
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            literal = value.value.strip()
            lowered = literal.lower()
            if any(token in lowered for token in PLACEHOLDER_TOKENS):
                continue
            if len(literal) >= 24 and re.fullmatch(r"[A-Za-z0-9_\-]+", literal):
                findings.add(literal)
    return sorted(findings)


def active_parameter_f1(row: dict[str, Any]) -> dict[str, Any]:
    doc = row.get("Parameter F1 Documentation")
    obs = row.get("Parameter F1 Observed")
    if isinstance(doc, dict) and doc.get("gold_count", 0):
        source = "documentation"
        selected = doc
    elif isinstance(obs, dict) and obs.get("gold_count", 0):
        source = "observed_code_params"
        selected = obs
    else:
        source = "none"
        selected = {"precision": None, "recall": None, "f1": None, "gold_count": 0, "pred_count": 0, "true_positive": 0}
    out = dict(selected)
    out["source"] = source
    return out


def parameter_ok(active_f1: dict[str, Any], threshold: float) -> bool:
    if not active_f1.get("gold_count"):
        return True
    value = active_f1.get("f1")
    return isinstance(value, (int, float)) and value >= threshold


def request_construction_pass(row: dict[str, Any], active_f1: dict[str, Any], threshold: float) -> bool:
    return bool(
        row.get("Host Accuracy")
        and row.get("Endpoint Accuracy")
        and row.get("Method Accuracy")
        and row.get("Header Accuracy")
        and parameter_ok(active_f1, threshold)
    )


def enrich_codegen(row: dict[str, Any], prompt_row: dict[str, Any], param_threshold: float) -> dict[str, Any]:
    code = row.get("解析后的代码") or ""
    gt = prompt_row.get("ground_truth") or {}
    dep = dependency_info(code)
    secrets = suspicious_secret_literals(code)
    active_f1 = active_parameter_f1(row)
    request_pass = request_construction_pass(row, active_f1, param_threshold)
    function_ok = function_name_accuracy(code, gt.get("function_name", ""))
    return_ok = return_value_handling(code)
    mock = row.get("Mock Execution") or {}
    executable_request = bool(mock.get("mock_execution_ran") and mock.get("mock_request_captured"))
    secret_safe = not secrets

    row.update({
        **dep,
        "Function Name Accuracy": function_ok,
        "Return Value Handling": return_ok,
        "Secret Safety": secret_safe,
        "Suspicious Secret Literals": secrets,
        "Active Parameter F1": active_f1,
        "Request Construction Pass": request_pass,
        "Executable Request Construction": executable_request,
        "Comment Grounding Proxy": bool(function_ok and request_pass),
        "Robust Invocation Pass": bool(
            row.get("Syntax Pass")
            and dep["Dependency Accuracy"]
            and secret_safe
            and return_ok
            and row.get("Error Handling Rate")
            and row.get("Mock Execution Pass")
        ),
        "新标准说明": (
            "Comment Grounding Proxy is an automatic proxy based on function-name, endpoint/method/header, "
            "and parameter alignment; it is not a replacement for human semantic audit."
        ),
    })
    return row


def normalize_endpoint_params(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,;]", value) if part.strip()]
    return []


def enrich_endpoint(row: dict[str, Any], prompt_row: dict[str, Any]) -> dict[str, Any]:
    parsed = row.get("parsed_answer") or {}
    gt = prompt_row.get("ground_truth") or {}
    top3 = parsed.get("top3_endpoint_ids") or parsed.get("top3") or parsed.get("top_3") or []
    top3_ok = isinstance(top3, list) and 1 <= len(top3) <= 3 and all(str(item).strip() for item in top3)
    params = normalize_endpoint_params(parsed.get("parameters"))
    gold_params = gt.get("documented_params") or gt.get("observed_params") or []
    row.update({
        "Endpoint Answer Format Pass": bool(parsed.get("endpoint_id") and parsed.get("method") and parsed.get("route")),
        "Endpoint Top3 Format Pass": top3_ok,
        "Endpoint Parameter Output Rate": bool(params) if gold_params else None,
        "Comment Grounding Proxy": bool(
            row.get("Top-1 Endpoint Accuracy")
            and row.get("Route Accuracy")
            and row.get("Method Accuracy")
        ),
        "Predicted Parameters": params,
        "Gold Parameters": gold_params,
        "新标准说明": (
            "Endpoint Comment Grounding Proxy checks whether the endpoint selected from the comment-derived task hint "
            "matches endpoint, route, and method ground truth."
        ),
    })
    return row


def bool_mean(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [row.get(key) for row in rows if isinstance(row.get(key), bool)]
    if not values:
        return None
    return sum(1 for value in values if value) / len(values)


def nested_f1_mean(rows: list[dict[str, Any]], key: str) -> float | None:
    values: list[float] = []
    for row in rows:
        value = row.get(key)
        if isinstance(value, dict) and isinstance(value.get("f1"), (float, int)):
            values.append(float(value["f1"]))
    if not values:
        return None
    return sum(values) / len(values)


def nullable_bool_mean(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [row.get(key) for row in rows if isinstance(row.get(key), bool)]
    if not values:
        return None
    return sum(1 for value in values if value) / len(values)


def summarize_flat(rows: list[dict[str, Any]], task: str) -> dict[str, Any]:
    if task == "codegen":
        bool_keys = [
            "Syntax Pass",
            "Function Name Accuracy",
            "Dependency Accuracy",
            "Host Accuracy",
            "Endpoint Accuracy",
            "Method Accuracy",
            "Header Accuracy",
            "Request Construction Pass",
            "Comment Grounding Proxy",
            "Return Value Handling",
            "Response Handling Rate",
            "Error Handling Rate",
            "Secret Safety",
            "Executable Request Construction",
            "Mock Execution Pass",
            "Robust Invocation Pass",
        ]
        out = {
            "任务": "新标准-注释引导API调用代码生成",
            "样本数": len(rows),
            **{key: bool_mean(rows, key) for key in bool_keys},
            "Active Parameter F1": nested_f1_mean(rows, "Active Parameter F1"),
            "Parameter F1 Documentation": nested_f1_mean(rows, "Parameter F1 Documentation"),
            "Parameter F1 Observed": nested_f1_mean(rows, "Parameter F1 Observed"),
        }
    else:
        bool_keys = [
            "Endpoint Answer Format Pass",
            "Endpoint Top3 Format Pass",
            "Top-1 Endpoint Accuracy",
            "Top-3 Endpoint Accuracy",
            "Route Accuracy",
            "Method Accuracy",
            "Comment Grounding Proxy",
        ]
        out = {
            "任务": "新标准-注释引导Endpoint选择",
            "样本数": len(rows),
            **{key: bool_mean(rows, key) for key in bool_keys},
            "Endpoint Parameter Output Rate": nullable_bool_mean(rows, "Endpoint Parameter Output Rate"),
            "Parameter F1": nested_f1_mean(rows, "Parameter F1"),
        }
    return out


def summarize(rows: list[dict[str, Any]], task: str) -> dict[str, Any]:
    out = summarize_flat(rows, task)
    out["按doc_match_type"] = {}

    for doc_type in sorted({row.get("doc_match_type", "unknown") for row in rows}):
        subset = [row for row in rows if row.get("doc_match_type", "unknown") == doc_type]
        out["按doc_match_type"][doc_type] = summarize_flat(subset, task)
    return out


def evaluate(args: argparse.Namespace) -> None:
    prompts = read_jsonl(Path(args.prompts))
    outputs = read_jsonl(Path(args.outputs))
    prompt_by_id = {str(row.get("样本ID")): row for row in prompts}

    rows: list[dict[str, Any]] = []
    for output_row in outputs:
        sample_id = str(output_row.get("样本ID"))
        prompt_row = prompt_by_id.get(sample_id)
        if not prompt_row:
            rows.append({"样本ID": sample_id, "评估错误": "prompt_not_found"})
            continue
        if args.task == "codegen":
            base = PIPELINE.evaluate_codegen_output(output_row, prompt_by_id)
            rows.append(enrich_codegen(base, prompt_row, args.param_threshold))
        else:
            base = PIPELINE.evaluate_endpoint_output(output_row, prompt_by_id)
            rows.append(enrich_endpoint(base, prompt_row))

    output_path = Path(args.output)
    write_json(output_path, rows)
    summary = summarize(rows, args.task)
    summary.update({
        "生成时间": PIPELINE.current_time(),
        "提示词文件": str(Path(args.prompts)),
        "模型输出文件": str(Path(args.outputs)),
        "评估结果文件": str(output_path),
        "参数匹配阈值": args.param_threshold if args.task == "codegen" else None,
        "说明": "These are deterministic automatic metrics. Comment Grounding Proxy should be paired with manual audit.",
    })
    write_json(output_path.with_name(output_path.stem + "_汇总.json"), summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def make_table(args: argparse.Namespace) -> None:
    result_dir = Path(args.result_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for path in sorted(result_dir.glob("新标准*_汇总.json")):
        data = load_json(path)
        row = {
            "模型": args.model_name,
            "文件": path.name,
            "任务": data.get("任务"),
            "样本数": data.get("样本数"),
        }
        for key, value in data.items():
            if isinstance(value, (int, float)) or value is None:
                row[key] = value
        rows.append(row)
    if not rows:
        raise FileNotFoundError(f"No 新标准 summary files found in {result_dir}")

    json_path = output_dir / "新测试标准_验证集结果汇总表.json"
    csv_path = output_dir / "新测试标准_验证集结果汇总表.csv"
    write_json(json_path, rows)
    fieldnames = sorted({key for row in rows for key in row})
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"json": str(json_path), "csv": str(csv_path), "rows": rows}, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="New evaluation standard for comment-guided RapidAPI code generation.")
    sub = parser.add_subparsers(dest="command", required=True)

    evaluate_cmd = sub.add_parser("evaluate")
    evaluate_cmd.add_argument("--task", choices=["codegen", "endpoint"], required=True)
    evaluate_cmd.add_argument("--prompts", required=True)
    evaluate_cmd.add_argument("--outputs", required=True)
    evaluate_cmd.add_argument("--output", required=True)
    evaluate_cmd.add_argument("--param-threshold", type=float, default=0.5)
    evaluate_cmd.set_defaults(func=evaluate)

    table_cmd = sub.add_parser("make-table")
    table_cmd.add_argument("--result-dir", required=True)
    table_cmd.add_argument("--output-dir", required=True)
    table_cmd.add_argument("--model-name", default="qwen2.5-coder:32b")
    table_cmd.set_defaults(func=make_table)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
