from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://api.deepseek.com/v1"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def existing_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            sample_id = row.get("样本ID") or row.get("sample_id")
            if sample_id:
                ids.add(str(sample_id))
    return ids


def chat_completion(
    prompt: str,
    model: str,
    base_url: str,
    api_key: str,
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout: int,
) -> str:
    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
    }
    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"No choices returned: {data}")
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run prompts against an OpenAI-compatible chat completion API.")
    parser.add_argument("--prompts", required=True, help="提示词 JSONL 文件")
    parser.add_argument("--output", required=True, help="模型输出 JSONL 文件；逐条追加，支持续跑")
    parser.add_argument("--model", required=True, help="模型名，例如 deepseek-chat 或本地 vLLM 服务中的模型名")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="OpenAI-compatible base URL, e.g. https://api.deepseek.com/v1")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY", help="从这个环境变量读取 API key；本地无鉴权服务可设为空字符串")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--max-tokens", type=int, default=1536)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--limit", type=int, default=0, help="调试时限制数量；正式实验保持 0")
    parser.add_argument("--sleep", type=float, default=0.0, help="每次请求后暂停秒数，便于控制 API 频率")
    args = parser.parse_args()

    prompt_rows = read_jsonl(Path(args.prompts))
    if args.limit:
        prompt_rows = prompt_rows[: args.limit]
    out_path = Path(args.output)
    done = existing_ids(out_path)
    api_key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    if args.api_key_env and not api_key:
        raise SystemExit(f"未找到环境变量 {args.api_key_env}。请先设置 API key，脚本不会保存 token。")

    remaining = [row for row in prompt_rows if str(row.get("样本ID")) not in done]
    print(json.dumps({
        "提示词总数": len(prompt_rows),
        "已存在输出": len(done),
        "待运行": len(remaining),
        "模型": args.model,
        "输出文件": str(out_path),
    }, ensure_ascii=False, indent=2))

    for index, row in enumerate(remaining, start=1):
        sample_id = str(row.get("样本ID"))
        prompt = str(row.get("提示词") or "")
        started = time.time()
        output = ""
        error = ""
        try:
            output = chat_completion(
                prompt=prompt,
                model=args.model,
                base_url=args.base_url,
                api_key=api_key,
                temperature=args.temperature,
                top_p=args.top_p,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
            )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        elapsed = time.time() - started
        append_jsonl(out_path, {
            "样本ID": sample_id,
            "模型名称": args.model,
            "提示词": prompt,
            "原始输出": output,
            "调用时间": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "temperature": args.temperature,
            "top_p": args.top_p,
            "max_tokens": args.max_tokens,
            "解析成功": bool(output and not error),
            "错误信息": error,
            "运行时间秒": elapsed,
            "base_url": args.base_url,
        })
        print(f"[{index}/{len(remaining)}] {sample_id} done, error={bool(error)}, elapsed={elapsed:.2f}s", flush=True)
        if args.sleep:
            time.sleep(args.sleep)


if __name__ == "__main__":
    main()
