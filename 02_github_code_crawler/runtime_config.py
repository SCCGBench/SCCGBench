"""
运行时路径与资源配置（单机版）。

提供爬虫所需的输出路径、token 与 API 列表获取等辅助函数。
"""

import os
import platform

BASE_PATH = os.path.dirname(os.path.abspath(__file__))
CURRENT_MACHINE = "local"


def get_runtime_info():
    """返回运行环境基本信息。"""
    return {
        "name": "local",
        "platform": platform.system().lower(),
        "base_path": BASE_PATH,
    }


def get_output_dir():
    d = os.path.join(BASE_PATH, "crawled_data")
    os.makedirs(d, exist_ok=True)
    return d


def get_progress_file():
    return os.path.join(get_output_dir(), ".progress.json")


def get_final_output():
    return os.path.join(get_output_dir(), "all_functions.json")


def get_log_file():
    return os.path.join(get_output_dir(), "crawler.log")


def get_rapidapi_metadata_dir():
    return os.path.join(BASE_PATH, "rapidapi_metadata", "by_category")


def filter_apis(all_apis: list) -> list:
    """单机版：使用全部 API。"""
    return all_apis


def load_tokens(all_tokens: list) -> list:
    """单机版：使用全部 token（过滤空值）。"""
    return [t.strip() for t in all_tokens if t and t.strip()]
