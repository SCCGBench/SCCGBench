#!/usr/bin/env python3
"""Fail-closed security checks for the public SCCGBench release.

The scanner never prints a matched value.  It reports only the file, sample,
JSON path, and finding type so that running the audit cannot leak a secret.
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "dataset"
PUBLIC_JSON = (
    DATASET / "api_documentation_mapping.json",
    DATASET / "sccgbench_3135.json",
    DATASET / "splits" / "train.json",
    DATASET / "splits" / "validation.json",
    DATASET / "splits" / "test.json",
)

FORBIDDEN_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "token.json",
    "tokens.json",
    "secrets.json",
    "api_key.json",
    "openai_key.txt",
    "deepseek_key.txt",
    "rapidapi_key.txt",
    "github_token.txt",
    "credentials.json",
    "service_account.json",
    "cookies.json",
    "config_private.py",
    "runtime_config_private.py",
    "local_config.py",
}
FORBIDDEN_PARTS = {
    "raw",
    "raw_data",
    "unredacted",
    "original_raw",
    "before_redaction",
    "user_data_dir",
    "browser_profile",
    ".auth",
    "Local Storage",
    "Session Storage",
}

STRONG_PATTERNS = {
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,255}\b"),
    "github_pat": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b"),
    "openai_style_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,255}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,255}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "huggingface_token": re.compile(r"\bhf_[A-Za-z0-9]{20,255}\b"),
    "jwt": re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    ),
    "private_key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
    ),
    "azure_storage_key": re.compile(
        r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{86}==(?=$|[^A-Za-z0-9+/=])"
    ),
    "gitlab_pat": re.compile(r"\bglpat-[A-Za-z0-9_-]{15,}\b"),
    "google_oauth_secret": re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b"),
    "stripe_key": re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    "sendgrid_key": re.compile(
        r"\bSG\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}\b"
    ),
    "telegram_bot_token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,40}\b"),
}

KEY_NAMES = (
    r"(?:x-rapidapi-key|authorization|api[_-]?key|apikey|x-api-key|key|"
    r"app[_-]?key|appkey|appid|app_id|token|auth[_-]?token|access[_-]?token|"
    r"access-token|refresh[_-]?token|secret|secret[_-]?key|access[_-]?key|"
    r"consumer[_-]?key|consumer[_-]?secret|client[_-]?secret|password|passwd|"
    r"cookie|session[_-]?id|sessionid)"
)
ASSIGNMENT = re.compile(
    rf"(?ix)\b({KEY_NAMES})\b[\"']?\s*[:=]\s*"
    rf"(?:[frub]{{0,2}})?([\"'])(.*?)\2"
)
QUERY_SECRET = re.compile(
    rf"(?i)(?:[?&]|[\"'])({KEY_NAMES})=([^&#\s\"']{{6,}})"
)
AUTH_LITERAL = re.compile(
    r"(?i)\b(?:Bearer|Basic)\s+([A-Za-z0-9._~+/=-]{8,})"
)
EMAIL = re.compile(
    r"(?i)(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])"
)
WINDOWS_USER_PATH = re.compile(
    r"(?i)[A-Z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s\"']+"
)
POSIX_USER_PATH = re.compile(
    r"(?:^|[\s\"'=(])/(?:home|Users)/[^/\s\"']+"
)
SAFE_VALUE = re.compile(
    r"(?i)(?:<REDACTED[^>]*>|YOUR(?:_[A-Z0-9]+)+|your[_-]|example|sample|"
    r"dummy|fake|placeholder|xxxx+|changeme|replace[_-]?me|not[_-]?set|"
    r"\*{4,}|\$\{|os\.getenv|process\.env|environ\[|getenv\(|"
    r"api[_-]?key$|token$|secret$|password$|key$)"
)


def entropy(value: str) -> float:
    counts = Counter(value)
    size = max(len(value), 1)
    return -sum((count / size) * math.log2(count / size) for count in counts.values())


def is_safe(value: str) -> bool:
    value = value.strip()
    return (
        len(value) < 6
        or bool(SAFE_VALUE.search(value))
        or bool(re.fullmatch(r"[xX*._-]+", value))
    )


def sensitive_literal(key: str, value: str) -> bool:
    value = re.sub(r"(?i)^(?:Bearer|Basic)\s+", "", value.strip()).strip()
    if is_safe(value):
        return False
    key = key.lower()
    if any(part in key for part in ("rapidapi", "authorization", "cookie", "session")):
        return True
    if any(part in key for part in ("password", "passwd", "token", "secret")):
        return True
    return len(value) >= 8 and entropy(value) >= 2.5


def audit_string(value: str, location: str, *, check_path: bool = True) -> list[str]:
    findings: list[str] = []
    for name, pattern in STRONG_PATTERNS.items():
        if pattern.search(value):
            findings.append(f"{location}: {name}")
    for match in ASSIGNMENT.finditer(value):
        if sensitive_literal(match.group(1), match.group(3)):
            findings.append(f"{location}: unredacted_{match.group(1).lower()}")
    for match in QUERY_SECRET.finditer(value):
        if sensitive_literal(match.group(1), match.group(2)):
            findings.append(f"{location}: query_{match.group(1).lower()}")
    for match in AUTH_LITERAL.finditer(value):
        if not is_safe(match.group(1)):
            findings.append(f"{location}: authorization_literal")
    if EMAIL.search(value):
        findings.append(f"{location}: email")
    if check_path:
        path_matches = list(WINDOWS_USER_PATH.finditer(value)) + list(POSIX_USER_PATH.finditer(value))
        if any("<REDACTED_USER>" not in match.group(0) for match in path_matches):
            findings.append(f"{location}: absolute_user_path")
    return findings


def walk(value: Any, location: str, findings: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_location = f"{location}.{key}"
            if isinstance(child, str) and re.fullmatch(KEY_NAMES, str(key), re.I):
                if sensitive_literal(str(key), child):
                    findings.append(f"{child_location}: sensitive_json_field")
            if isinstance(child, str):
                findings.extend(
                    audit_string(
                        child,
                        child_location,
                        check_path=str(key) not in {"file_url", "repo_url", "html_url"},
                    )
                )
            else:
                walk(child, child_location, findings)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            walk(child, f"{location}[{index}]", findings)


def main() -> int:
    findings: list[str] = []

    for path in ROOT.rglob("*"):
        if ".git" in path.parts:
            continue
        relative = path.relative_to(ROOT)
        if path.name in FORBIDDEN_NAMES or any(part in FORBIDDEN_PARTS for part in relative.parts):
            findings.append(f"{relative.as_posix()}: forbidden_release_path")
        if path.is_file() and (
            path.name.endswith("_raw.json")
            or path.name.endswith("_raw.jsonl")
            or "unredacted" in path.name.lower()
            or "before_redaction" in path.name.lower()
        ):
            findings.append(f"{relative.as_posix()}: forbidden_release_path")

    loaded: dict[Path, Any] = {}
    for path in PUBLIC_JSON:
        try:
            loaded[path] = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - release guard
            findings.append(f"{path.relative_to(ROOT).as_posix()}: invalid_json:{type(exc).__name__}")
            continue
        walk(loaded[path], path.relative_to(ROOT).as_posix(), findings)

    full = loaded.get(DATASET / "sccgbench_3135.json")
    if isinstance(full, list):
        full_ids = {item.get("sample_id") for item in full if isinstance(item, dict)}
        if len(full) != 3135 or len(full_ids) != 3135:
            findings.append("dataset/sccgbench_3135.json: unexpected_sample_count")
        split_ids: list[set[str]] = []
        expected = {"train": 2257, "validation": 266, "test": 612}
        for name, count in expected.items():
            split = loaded.get(DATASET / "splits" / f"{name}.json")
            if not isinstance(split, list) or len(split) != count:
                findings.append(f"dataset/splits/{name}.json: unexpected_sample_count")
                continue
            split_ids.append({item.get("sample_id") for item in split if isinstance(item, dict)})
        if len(split_ids) == 3:
            if set().union(*split_ids) != full_ids:
                findings.append("dataset/splits: union_does_not_match_full_dataset")
            if split_ids[0] & split_ids[1] or split_ids[0] & split_ids[2] or split_ids[1] & split_ids[2]:
                findings.append("dataset/splits: overlapping_sample_ids")

    if findings:
        print(f"PUBLIC RELEASE AUDIT: FAIL ({len(findings)} findings)")
        for finding in findings[:200]:
            print(f"- {finding}")
        if len(findings) > 200:
            print(f"- ... {len(findings) - 200} additional findings omitted")
        return 1

    print("PUBLIC RELEASE AUDIT: PASS")
    print("- JSON structure and split integrity: PASS")
    print("- Strong credential signatures: 0")
    print("- Unredacted sensitive literals: 0")
    print("- Email and absolute-user-path candidates in published JSON: 0")
    print("- Forbidden raw/config/session paths: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
