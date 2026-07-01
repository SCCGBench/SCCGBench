"""
增强版 GitHub 代码爬虫：从 GitHub 抽取真实的 API 调用函数。

核心特性:
1. 优先爬取有注释/docstring 的代码
2. 多层查询策略（endpoint/route/host/header/library/function）提高覆盖率
3. 工具生成代码检测，剔除模板/包装代码
4. 代码相似度检测，避免重复
5. 智能 Token 轮换 - 自动检测限流并切换 token
6. 质量打分与过滤
"""

import json
import os
import re
import time
import ast
import logging
import threading
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse
from difflib import SequenceMatcher
import requests
import warnings
warnings.filterwarnings('ignore', category=SyntaxWarning)
# 导入配置
import sys

# 导入运行时路径配置
from runtime_config import (
    get_runtime_info,
    get_output_dir,
    get_progress_file,
    get_final_output,
    get_log_file,
    get_rapidapi_metadata_dir,
    filter_apis,
    load_tokens,
    CURRENT_MACHINE
)

# 导入基础配置
from config import *

# ============================================================================
# 日志配置
# ============================================================================

# 确保输出目录存在
os.makedirs(get_output_dir(), exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(get_log_file(), encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 记录运行环境信息
runtime_info = get_runtime_info()
logger.info(f"=" * 80)
logger.info(f"平台: {runtime_info['platform']}")
logger.info(f"基础路径: {runtime_info['base_path']}")
logger.info(f"=" * 80)

# ============================================================================
# GitHub Token管理
# ============================================================================

class TokenManager:
    """增强版GitHub Token轮换管理器 - 支持限流检测和自动恢复"""

    def __init__(self, tokens: List[str]):
        self.tokens = [t.strip() for t in tokens if t and t.strip()]
        self.current_index = 0
        self.token_usage = {token: 0 for token in self.tokens}
        self.lock = threading.RLock()

        # Token状态跟踪
        self.token_status = {token: 'active' for token in self.tokens}  # active, rate_limited, error
        self.token_rate_limit_until = {token: 0 for token in self.tokens}  # 限流恢复时间戳
        self.token_error_count = {token: 0 for token in self.tokens}  # 错误计数

        logger.info(f"TokenManager初始化: {len(self.tokens)} 个有效tokens")

    def _refresh_token_status(self, token: str):
        """恢复已经过冷却时间的限流/错误token。"""
        if self.token_status[token] in ('rate_limited', 'error'):
            if time.time() >= self.token_rate_limit_until[token]:
                self.token_status[token] = 'active'
                self.token_error_count[token] = 0

    def get_current_token(self) -> str:
        """获取当前可用token"""
        with self.lock:
            if not self.tokens:
                raise RuntimeError("没有可用的GitHub token")

            # 检查当前token是否可用
            current_token = self.tokens[self.current_index]
            self._refresh_token_status(current_token)

            # 如果当前token不可用，自动切换到下一个可用token
            if self.token_status[current_token] != 'active':
                self._find_next_available_token()
                current_token = self.tokens[self.current_index]

            return current_token

    def get_request_token(self) -> str:
        """获取本次请求使用的token，并立即轮换到下一个可用token。"""
        with self.lock:
            token = self.get_current_token()
            self.token_usage[token] += 1
            self.current_index = (self.current_index + 1) % len(self.tokens)
            return token

    def _find_next_available_token(self):
        """查找下一个可用的token"""
        if not self.tokens:
            raise RuntimeError("没有可用的GitHub token")

        start_index = self.current_index
        attempts = 0

        while attempts < len(self.tokens):
            self.current_index = (self.current_index + 1) % len(self.tokens)
            attempts += 1

            token = self.tokens[self.current_index]

            self._refresh_token_status(token)

            # 找到可用token
            if self.token_status[token] == 'active':
                if self.current_index != start_index:
                    logger.info(f"切换到 token {self.current_index + 1}/{len(self.tokens)}")
                return

        # 所有token都不可用，等待最早恢复的token
        logger.warning("所有tokens都被限流，等待恢复...")
        min_wait_time = min(self.token_rate_limit_until.values())
        wait_seconds = max(min_wait_time - time.time(), 60)
        logger.info(f"等待 {int(wait_seconds)} 秒后重试...")
        time.sleep(wait_seconds)

        # 重置所有token状态
        for token in self.tokens:
            self._refresh_token_status(token)

    def mark_rate_limited(self, reset_timestamp: int = None, token: str = None):
        """标记当前token为限流状态"""
        with self.lock:
            current_token = token or self.tokens[self.current_index]
            self.token_status[current_token] = 'rate_limited'

            # 设置恢复时间（默认1小时后）
            if reset_timestamp:
                self.token_rate_limit_until[current_token] = reset_timestamp
            else:
                self.token_rate_limit_until[current_token] = time.time() + 3600

            recovery_time = datetime.fromtimestamp(self.token_rate_limit_until[current_token])
            logger.warning(f"Token被限流，预计恢复时间: {recovery_time.strftime('%H:%M:%S')}")

            # 自动切换到下一个token
            self._find_next_available_token()

    def mark_error(self, token: str = None):
        """标记当前token出错"""
        with self.lock:
            current_token = token or self.tokens[self.current_index]
            self.token_error_count[current_token] += 1

            # 如果错误次数过多，暂时禁用该token
            if self.token_error_count[current_token] >= 5:
                logger.warning("Token错误次数过多，暂时禁用")
                self.token_status[current_token] = 'error'
                self.token_rate_limit_until[current_token] = time.time() + 600  # 10分钟后重试

    def rotate_token(self):
        """手动轮换到下一个token"""
        with self.lock:
            self._find_next_available_token()

    def record_usage(self):
        """记录token使用"""
        # v3.1中请求token在get_request_token里已计数；保留该方法兼容旧调用。
        return

    def get_status_summary(self) -> Dict:
        """获取token状态摘要"""
        with self.lock:
            active_count = sum(1 for status in self.token_status.values() if status == 'active')
            limited_count = sum(1 for status in self.token_status.values() if status == 'rate_limited')
            error_count = sum(1 for status in self.token_status.values() if status == 'error')

            return {
                'total': len(self.tokens),
                'active': active_count,
                'rate_limited': limited_count,
                'error': error_count,
                'current_index': self.current_index + 1
            }

token_manager = TokenManager(load_tokens(GITHUB_TOKENS))

# ============================================================================
# 速率限制器
# ============================================================================

class RateLimiter:
    """GitHub API速率限制器"""

    def __init__(self, max_calls_per_minute=500):
        self.max_calls = max_calls_per_minute
        self.calls = []
        self.lock = threading.Lock()

    def wait_if_needed(self):
        with self.lock:
            now = datetime.now()
            self.calls = [t for t in self.calls if (now - t).seconds < 60]

            if len(self.calls) >= self.max_calls:
                oldest_call = min(self.calls)
                wait_time = 60 - (now - oldest_call).seconds
                if wait_time > 0:
                    logger.info(f"Rate limit reached, waiting {wait_time}s...")
                    time.sleep(wait_time + 1)
                    self.calls = []

            self.calls.append(now)

rate_limiter = RateLimiter(max_calls_per_minute=500)

# ============================================================================
# 注释分析工具
# ============================================================================

def count_comment_lines(code: str) -> int:
    """统计代码中的注释行数"""
    lines = code.split('\n')
    comment_count = 0

    for line in lines:
        stripped = line.strip()
        # 单行注释
        if stripped.startswith('#') and not stripped.startswith('#!'):
            comment_count += 1

    return comment_count

def has_docstring(code: str) -> bool:
    """检查函数是否有docstring"""
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                docstring = ast.get_docstring(node)
                if docstring:
                    return True
    except:
        pass
    return False

def calculate_comment_score(code: str) -> float:
    """计算注释得分"""
    score = 0.0

    # 统计注释行
    comment_lines = count_comment_lines(code)
    if comment_lines > 0:
        score += min(comment_lines * COMMENT_BONUS_PER_LINE, MAX_COMMENT_BONUS)

    # 检查docstring
    if has_docstring(code):
        score += DOCSTRING_BONUS

    return score

# ============================================================================
# 代码相似度检测
# ============================================================================

class SimilarityDetector:
    """代码相似度检测器"""

    def __init__(self, threshold=CODE_SIMILARITY_THRESHOLD):
        self.threshold = threshold
        self.seen_codes = []

    def is_similar(self, code: str) -> bool:
        """检查代码是否与已有代码相似"""
        for seen_code in self.seen_codes:
            similarity = SequenceMatcher(None, code, seen_code).ratio()
            if similarity > self.threshold:
                return True
        return False

    def add_code(self, code: str):
        """添加代码到已见列表"""
        self.seen_codes.append(code)

    def clear(self):
        """清空已见代码（每个API重置）"""
        self.seen_codes = []

similarity_detector = SimilarityDetector()

# v3.1: 当前进程内缓存，避免重复下载同一文件或重复查询同一仓库信息。
repo_info_cache = {}
file_content_cache = {}
repo_info_cache_lock = threading.Lock()
file_content_cache_lock = threading.Lock()

# ============================================================================
# RapidAPI元数据加载
# ============================================================================

def load_rapidapi_metadata_by_category(category_dir: str) -> List[Dict]:
    """从分类目录加载所有RapidAPI元数据"""
    all_apis = []

    if not os.path.exists(category_dir):
        logger.error(f"目录不存在: {category_dir}")
        return []

    category_files = sorted([f for f in os.listdir(category_dir) if f.endswith('.json')])
    logger.info(f"找到 {len(category_files)} 个类别文件")

    for filename in category_files:
        filepath = os.path.join(category_dir, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)

            apis = data.get('apis', [])
            all_apis.extend(apis)

        except Exception as e:
            logger.error(f"加载失败: {filename} - {e}")
            continue

    logger.info(f"总共加载 {len(all_apis)} 个API\n")
    return all_apis

# ============================================================================
# GitHub搜索查询构建
# ============================================================================

def extract_api_hosts(api_data: Dict) -> List[str]:
    """提取API的所有hosts"""
    hosts = []
    for endpoint in api_data.get('endpoints_metadata', []):
        url = endpoint.get('url', '')
        if url:
            parsed = urlparse(url)
            if parsed.netloc:
                hosts.append(parsed.netloc)
    return list(set(hosts))

def normalize_endpoint_route(route: Any) -> str:
    """标准化RapidAPI endpoint route，方便和代码做轻量匹配。"""
    if not route:
        return ''
    route = str(route).strip()
    if not route:
        return ''
    return route if route.startswith('/') else f'/{route}'

def unique_keep_order(values: List[str]) -> List[str]:
    """去重但保留原顺序。"""
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result

def endpoint_route_variants(route: Any) -> List[str]:
    """生成route匹配变体，兼容代码中是否保留末尾斜杠。"""
    normalized = normalize_endpoint_route(route)
    if not normalized or normalized == '/':
        return []

    variants = [normalized]
    if normalized.endswith('/') and len(normalized) > 1:
        variants.append(normalized.rstrip('/'))
    else:
        variants.append(f'{normalized}/')

    return unique_keep_order(variants)

def endpoint_url_variants(endpoint: Dict) -> List[str]:
    """生成完整endpoint URL匹配变体。"""
    url = (endpoint.get('url') or '').strip()
    variants = [url]
    if url.endswith('/'):
        variants.append(url.rstrip('/'))
    elif url:
        variants.append(f'{url}/')
    return unique_keep_order(variants)

def endpoint_route_value(endpoint: Dict) -> str:
    """取endpoint route；元数据缺route时从url path兜底。"""
    route = normalize_endpoint_route(endpoint.get('route', ''))
    if route:
        return route
    return normalize_endpoint_route(urlparse(endpoint.get('url', '')).path)

def endpoint_has_meaningful_route(endpoint: Dict) -> bool:
    """过滤根路径或过短route，避免endpoint查询退化成host查询。"""
    route = endpoint_route_value(endpoint)
    min_len = int(globals().get('MIN_ENDPOINT_ROUTE_LENGTH', 4))
    return bool(route and route != '/' and len(route) >= min_len)

def endpoint_sort_key(endpoint: Dict) -> Tuple:
    """优先查询更可能代表真实接口的endpoint。"""
    has_description = bool(endpoint.get('endpoint_description') or endpoint.get('description'))
    has_params = any(endpoint.get(key) for key in ('params', 'path_params', 'header_params', 'payload'))
    index = endpoint.get('index')
    try:
        index_value = int(index)
    except (TypeError, ValueError):
        index_value = 999999
    return (not has_description, not has_params, index_value)

def get_endpoint_query_candidates(api_data: Dict) -> List[Dict]:
    """选取少量文档endpoint做精确搜索，兼顾覆盖率和速度。"""
    endpoints = [
        endpoint for endpoint in (api_data.get('endpoints_metadata', []) or [])
        if endpoint_has_meaningful_route(endpoint)
    ]
    endpoints = sorted(endpoints, key=endpoint_sort_key)

    max_endpoints = int(globals().get('MAX_ENDPOINTS_PER_API_FOR_QUERIES', 2))
    selected = []
    seen = set()
    for endpoint in endpoints:
        key = endpoint.get('endpoint_id') or endpoint.get('url') or endpoint.get('route')
        if not key or key in seen:
            continue
        seen.add(key)
        selected.append(endpoint)
        if len(selected) >= max_endpoints:
            break
    return selected

def select_documented_endpoint(code: str, api_data: Dict, hosts: List[str]) -> Tuple[Dict, str]:
    """选择和函数代码最相关的RapidAPI文档endpoint。"""
    endpoints = api_data.get('endpoints_metadata', []) or []
    if not endpoints:
        return {}, 'none'

    for endpoint in endpoints:
        if any(url and url in code for url in endpoint_url_variants(endpoint)):
            return endpoint, 'url'

    for endpoint in endpoints:
        if any(route and route in code for route in endpoint_route_variants(endpoint_route_value(endpoint))):
            return endpoint, 'route'

    for endpoint in endpoints:
        endpoint_host = endpoint.get('rapidapi_host', '')
        if endpoint_host and endpoint_host in code:
            return endpoint, 'host_only'

    for host in hosts:
        if host and host in code:
            return endpoints[0], 'host_only'

    return endpoints[0], 'none'

def build_api_metadata(endpoint: Dict, code_params: Dict, doc_match_type: str) -> Dict:
    """构建每条函数携带的API文档关联信息。"""
    return {
        'url': endpoint.get('url', ''),
        'method': endpoint.get('method', 'GET'),
        'headers': endpoint.get('headers', {}),
        'params': endpoint.get('params'),
        'params_source': 'documentation',
        'path_params': endpoint.get('path_params'),
        'header_params': endpoint.get('header_params'),
        'payload': endpoint.get('payload'),
        'route': endpoint_route_value(endpoint),
        'endpoint_id': endpoint.get('endpoint_id', ''),
        'endpoint_name': endpoint.get('endpoint_name', ''),
        'doc_match_type': doc_match_type,
        'code_params': code_params,
    }

def build_github_search_queries(api_data: Dict) -> List[Dict[str, str]]:
    """构建多层次GitHub搜索查询 - 优化版：更宽松的搜索条件"""
    queries = []
    hosts = extract_api_hosts(api_data)

    if not hosts:
        return queries

    primary_host = hosts[0]

    # 提取API关键词（去掉.p.rapidapi.com后缀）
    api_keyword = primary_host.replace('.p.rapidapi.com', '').replace('-', ' ')
    api_name_parts = primary_host.split('.')[0].split('-')

    # Layer 0: 文档endpoint优先匹配，提升代码和API文档的强映射覆盖率。
    endpoint_candidates = get_endpoint_query_candidates(api_data)
    for endpoint in endpoint_candidates:
        if 'endpoint_url_match' in QUERY_STRATEGIES:
            for url in endpoint_url_variants(endpoint)[:1]:
                queries.append({
                    'query': f'"{url}" language:python',
                    'layer': 'endpoint_url_match',
                    'endpoint_url': url,
                    'endpoint_route': endpoint_route_value(endpoint),
                })

        if 'endpoint_route_host_match' in QUERY_STRATEGIES:
            for route in endpoint_route_variants(endpoint_route_value(endpoint))[:1]:
                queries.append({
                    'query': f'"{route}" "{primary_host}" language:python',
                    'layer': 'endpoint_route_host_match',
                    'endpoint_url': endpoint.get('url', ''),
                    'endpoint_route': route,
                })

    # Layer 1: 完整域名匹配（保留原逻辑）
    if 'host_match' in QUERY_STRATEGIES:
        queries.append({
            'query': f'"{primary_host}" language:python',
            'layer': 'host_match'
        })

    # Layer 2: RapidAPI Header匹配（保留原逻辑）
    if 'header_match' in QUERY_STRATEGIES:
        queries.append({
            'query': f'"x-rapidapi-host" "{primary_host}" language:python',
            'layer': 'header_match'
        })

    # Layer 3: API关键词 + rapidapi（新增：更宽松）
    if 'keyword_match' in QUERY_STRATEGIES:
        # 使用API名称的主要部分
        main_keyword = api_name_parts[0] if api_name_parts else primary_host.split('.')[0]
        if len(main_keyword) > 3:  # 避免太短的关键词
            queries.append({
                'query': f'"{main_keyword}" "rapidapi" language:python',
                'layer': 'keyword_match',
                'keyword': main_keyword,
            })

    # Layer 4: 域名 + requests（放宽条件）
    if 'library_match' in QUERY_STRATEGIES:
        queries.append({
            'query': f'"{primary_host}" "requests" language:python',
            'layer': 'library_match'
        })

    # Layer 5: 域名 + python函数（放宽条件）
    if 'function_match' in QUERY_STRATEGIES:
        queries.append({
            'query': f'"{primary_host}" "def " language:python',
            'layer': 'function_match'
        })

    # Layer 6: API关键词 + requests（新增：更宽松）
    if 'keyword_requests_match' in QUERY_STRATEGIES:
        main_keyword = api_name_parts[0] if api_name_parts else primary_host.split('.')[0]
        if len(main_keyword) > 3:
            queries.append({
                'query': f'"{main_keyword}" "requests.post" OR "requests.get" language:python',
                'layer': 'keyword_requests_match',
                'keyword': main_keyword,
            })

    # Layer 7: 带错误处理的代码（保留但放宽）
    if 'error_handling_match' in QUERY_STRATEGIES:
        queries.append({
            'query': f'"{primary_host}" language:python "try"',
            'layer': 'error_handling_match'
        })

    deduped_queries = []
    seen_queries = set()
    for query_info in queries:
        key = (query_info.get('query'), query_info.get('layer'))
        if key in seen_queries:
            continue
        seen_queries.add(key)
        deduped_queries.append(query_info)

    return deduped_queries

# ============================================================================
# GitHub API交互
# ============================================================================

def search_github_code(query: str, max_results: int = 100) -> List[Dict]:
    """搜索GitHub代码 - 限制最多返回50个文件"""
    rate_limiter.wait_if_needed()
    token = token_manager.get_request_token()

    # 限制最大结果数为50
    max_results = min(max_results, 100)

    url = f"{GITHUB_API_BASE}/search/code"
    headers = {
        'Accept': 'application/vnd.github.v3+json',
        'Authorization': f'token {token}'
    }

    params = {
        'q': query,
        'per_page': min(max_results, 100),
        'sort': 'indexed'
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        token_manager.record_usage()

        # 处理限流
        if response.status_code == 403:
            reset_time = int(response.headers.get('X-RateLimit-Reset', 0))
            if reset_time > 0:
                token_manager.mark_rate_limited(reset_time, token=token)
            else:
                token_manager.mark_rate_limited(token=token)
            return []

        # 处理其他错误
        if response.status_code != 200:
            logger.error(f"Search failed with status {response.status_code}")
            token_manager.mark_error(token=token)
            time.sleep(RATE_LIMIT_DELAY)
            return []

        response.raise_for_status()
        data = response.json()

        time.sleep(RATE_LIMIT_DELAY)
        items = data.get('items', [])

        # 确保不超过50个文件
        return items[:max_results]

    except Exception as e:
        logger.error(f"Search error: {e}")
        token_manager.mark_error(token=token)
        time.sleep(RATE_LIMIT_DELAY)
        return []

def get_repo_info(repo_full_name: str) -> Optional[Dict]:
    """获取仓库信息"""
    with repo_info_cache_lock:
        if repo_full_name in repo_info_cache:
            return repo_info_cache[repo_full_name]

    rate_limiter.wait_if_needed()
    token = token_manager.get_request_token()

    try:
        url = f"{GITHUB_API_BASE}/repos/{repo_full_name}"
        headers = {
            'Accept': 'application/vnd.github.v3+json',
            'Authorization': f'token {token}'
        }

        response = requests.get(url, headers=headers, timeout=30)
        token_manager.record_usage()

        if response.status_code == 200:
            data = response.json()
            repo_info = {
                'stars': data.get('stargazers_count', 0),
                'forks': data.get('forks_count', 0),
                'watchers': data.get('watchers_count', 0),
                'open_issues': data.get('open_issues_count', 0),
                'created_at': data.get('created_at', ''),
                'updated_at': data.get('updated_at', ''),
                'description': data.get('description', ''),
                'language': data.get('language', ''),
                'license': data.get('license', {}).get('name', '') if data.get('license') else '',
                'topics': data.get('topics', []),
                'is_fork': data.get('fork', False),
            }
            with repo_info_cache_lock:
                repo_info_cache[repo_full_name] = repo_info
            return repo_info
        if response.status_code == 403:
            reset_time = int(response.headers.get('X-RateLimit-Reset', 0))
            token_manager.mark_rate_limited(reset_time if reset_time > 0 else None, token=token)
        else:
            token_manager.mark_error(token=token)
        with repo_info_cache_lock:
            repo_info_cache[repo_full_name] = None
        return None
    except:
        token_manager.mark_error(token=token)
        with repo_info_cache_lock:
            repo_info_cache[repo_full_name] = None
        return None

def get_file_content(repo_full_name: str, file_path: str) -> Optional[str]:
    """获取文件内容"""
    cache_key = (repo_full_name, file_path)
    with file_content_cache_lock:
        if cache_key in file_content_cache:
            return file_content_cache[cache_key]

    rate_limiter.wait_if_needed()
    token = token_manager.get_request_token()

    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/contents/{file_path}"
    headers = {
        'Accept': 'application/vnd.github.v3.raw',
        'Authorization': f'token {token}'
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        token_manager.record_usage()

        if response.status_code == 200:
            with file_content_cache_lock:
                file_content_cache[cache_key] = response.text
            return response.text
        if response.status_code == 403:
            reset_time = int(response.headers.get('X-RateLimit-Reset', 0))
            token_manager.mark_rate_limited(reset_time if reset_time > 0 else None, token=token)
        elif response.status_code != 404:
            token_manager.mark_error(token=token)
        with file_content_cache_lock:
            file_content_cache[cache_key] = None
        return None
    except:
        token_manager.mark_error(token=token)
        with file_content_cache_lock:
            file_content_cache[cache_key] = None
        return None

# ============================================================================
# 代码质量检查
# ============================================================================

def is_auto_generated_code(code: str, repo_name: str) -> bool:
    """检测是否为自动生成的代码"""
    code_lower = code.lower()
    repo_lower = repo_name.lower()

    for pattern in AUTO_GEN_PATTERNS:
        # 如果是正则表达式
        if pattern.startswith('r\'') or pattern.startswith('r"'):
            pattern_str = pattern[2:-1]
            if re.search(pattern_str, code) or re.search(pattern_str, repo_name):
                return True
        # 普通字符串匹配
        else:
            if pattern.lower() in code_lower or pattern.lower() in repo_lower:
                return True

    return False

def check_code_quality(code: str) -> Dict[str, Any]:
    """检查代码质量（不修改代码）"""
    checks = {
        'has_imports': False,
        'has_api_call': False,
        'has_response_handling': False,
        'has_error_handling': False,
        'is_syntactically_valid': False,
        'has_database_call': False,
        'has_indentation_issue': False,
        'has_comments': False,
        'has_docstring': False,
        'comment_lines': 0,
    }

    # 检查导入
    if re.search(r'^\s*import\s+|^\s*from\s+', code, re.MULTILINE):
        checks['has_imports'] = True

    # 检查API调用
    if any(p in code for p in ['requests.', 'urllib.', 'httpx.', 'http.client']):
        checks['has_api_call'] = True

    # 检查响应处理
    if any(p in code for p in ['.json()', '.text', '.content', 'response.status_code', 'response[']):
        checks['has_response_handling'] = True

    # 检查错误处理
    if 'try:' in code and 'except' in code:
        checks['has_error_handling'] = True

    # 检查数据库调用
    if any(p in code for p in ['mysql', 'sqlite', 'cursor.', '.execute(', 'psycopg2', 'pymongo']):
        checks['has_database_call'] = True

    # 检查缩进问题（类方法片段）
    lines = code.split('\n')
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith('def ') and line[0] in (' ', '\t'):
            checks['has_indentation_issue'] = True
            break

    # 检查注释
    comment_lines = count_comment_lines(code)
    checks['comment_lines'] = comment_lines
    checks['has_comments'] = comment_lines > 0

    # 检查docstring
    checks['has_docstring'] = has_docstring(code)

    # 检查语法
    try:
        ast.parse(code)
        checks['is_syntactically_valid'] = True
    except:
        pass

    return checks

def calculate_quality_score(code: str, repo_info: Optional[Dict], checks: Dict) -> float:
    """计算质量评分 v3.0 - 增加注释权重"""
    score = 0.0

    # 1. 语法有效性
    if checks['is_syntactically_valid']:
        score += QUALITY_SCORING['syntax_valid']

    # 2. API调用
    if checks['has_api_call']:
        score += QUALITY_SCORING['has_api_call']

    # 3. 响应处理
    if checks['has_response_handling']:
        score += QUALITY_SCORING['has_response_handling']

    # 4. 错误处理
    if checks['has_error_handling']:
        score += QUALITY_SCORING['has_error_handling']

    # 5. 代码长度
    lines = code.split('\n')
    loc = len([l for l in lines if l.strip()])
    if 10 <= loc <= 30:
        score += QUALITY_SCORING['code_length'] * 0.5
    elif 30 < loc <= 50:
        score += QUALITY_SCORING['code_length']
    elif loc > 50:
        score += QUALITY_SCORING['code_length'] * 0.8

    # 6. 仓库质量
    if repo_info:
        stars = repo_info.get('stars', 0)
        if stars >= 100:
            score += QUALITY_SCORING['repo_quality']
        elif stars >= 10:
            score += QUALITY_SCORING['repo_quality'] * 0.6
        elif stars >= 1:
            score += QUALITY_SCORING['repo_quality'] * 0.3

    # 7. 注释加分（新增）
    comment_score = calculate_comment_score(code)
    score += min(comment_score, QUALITY_SCORING['has_comments'])

    # 扣分项
    if checks['has_database_call']:
        score += QUALITY_PENALTIES['has_database_call']

    if checks['has_indentation_issue']:
        score += QUALITY_PENALTIES['has_indentation_issue']

    if not checks['has_imports']:
        score += QUALITY_PENALTIES['missing_imports']

    return max(0.0, min(100.0, score))

# ============================================================================
# 函数提取
# ============================================================================

def extract_functions_from_code(code: str, api_hosts: List[str]) -> List[Dict]:
    """从代码中提取函数"""
    try:
        tree = ast.parse(code)
    except:
        return []

    functions = []
    lines = code.split('\n')

    # 提取导入语句
    imports = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if node.lineno - 1 < len(lines):
                imports.append(lines[node.lineno - 1])

    # 提取函数
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            func_start = node.lineno - 1
            func_end = node.end_lineno
            func_code = '\n'.join(lines[func_start:func_end])

            # 检查是否包含API host
            if any(host in func_code for host in api_hosts):
                # 保持原始代码不变
                complete_code = '\n\n'.join(imports + [func_code])
                functions.append({
                    'name': node.name,
                    'code': complete_code,
                    'start_line': func_start,
                    'end_line': func_end,
                })

    return functions

def extract_api_parameters(code: str) -> Dict[str, Any]:
    """从代码中提取API参数"""
    params = {}

    # 提取headers
    headers_match = re.search(r'headers\s*=\s*\{([^}]+)\}', code, re.DOTALL)
    if headers_match:
        params['headers_code'] = headers_match.group(0)

    # 提取params/querystring
    params_match = re.search(r'params\s*=\s*\{([^}]+)\}', code, re.DOTALL)
    if params_match:
        params['params_code'] = params_match.group(0)

    querystring_match = re.search(r'querystring\s*=\s*\{([^}]+)\}', code, re.DOTALL)
    if querystring_match:
        params['querystring_code'] = querystring_match.group(0)

    return params

# ============================================================================
# 主爬取逻辑
# ============================================================================

def rank_functions_by_quality(functions: List[Tuple[Dict, float]]) -> List[Tuple[Dict, float]]:
    """对函数按质量分排序"""
    match_priority = {'url': 3, 'route': 2, 'host_only': 1, 'none': 0}
    return sorted(
        functions,
        key=lambda x: (
            match_priority.get(x[0].get('api_metadata', {}).get('doc_match_type', 'none'), 0),
            x[1],
            bool(x[0].get('code', {}).get('has_comments', False)),
            int(x[0].get('code', {}).get('comment_lines', 0) or 0),
        ),
        reverse=True,
    )

def prioritize_search_results(search_results: List[Dict]) -> List[Dict]:
    """优先排序搜索结果 - 高Star仓库优先"""
    return sorted(search_results,
                  key=lambda x: x.get('repository', {}).get('stargazers_count', 0),
                  reverse=True)

def is_broad_query_layer(layer: str) -> bool:
    """判断是否为宽泛关键词查询层。"""
    return layer in globals().get('BROAD_QUERY_LAYERS', set())

def is_endpoint_query_layer(layer: str) -> bool:
    """判断是否为文档endpoint精确查询层。"""
    return layer in globals().get('ENDPOINT_QUERY_LAYERS', set())

def get_max_results_for_query_layer(layer: str) -> int:
    """按查询层控制结果数量，endpoint和宽泛查询都更克制。"""
    if is_endpoint_query_layer(layer):
        return int(globals().get('MAX_ENDPOINT_RESULTS_PER_QUERY', MAX_RESULTS_PER_QUERY))
    if is_broad_query_layer(layer):
        return int(globals().get('MAX_BROAD_RESULTS_PER_QUERY', MAX_RESULTS_PER_QUERY))
    return MAX_RESULTS_PER_QUERY

def should_skip_broad_query(query_info: Dict[str, str]) -> bool:
    """跳过过于泛化的关键词查询，避免处理大量无关文件。"""
    keyword = (query_info.get('keyword') or '').strip().lower()
    generic_keywords = globals().get('GENERIC_BROAD_KEYWORDS', set())
    return bool(keyword and keyword in generic_keywords)

def has_target_host(code: str, hosts: List[str]) -> bool:
    """文件中是否包含目标 RapidAPI host。"""
    return any(host in code for host in hosts)

def count_selectable_candidates(repo_candidates: Dict[str, List[Tuple[Dict, float]]]) -> int:
    """按每仓库上限估算当前可选函数数量。"""
    return sum(min(len(candidates), MAX_FUNCTIONS_PER_REPO) for candidates in repo_candidates.values())

def crawl_api_functions(api_data: Dict) -> List[Dict]:
    """爬取单个API的函数"""
    api_name = api_data.get('api_name', 'unknown')

    logger.info(f"\n{'='*80}")
    logger.info(f"API: {api_name}")
    logger.info(f"{'='*80}")

    # 提取API hosts
    hosts = extract_api_hosts(api_data)

    if not hosts:
        logger.warning("无法提取API host")
        return []

    queries = build_github_search_queries(api_data)
    precise_queries = [q for q in queries if not is_broad_query_layer(q.get('layer', ''))]
    broad_queries = [q for q in queries if is_broad_query_layer(q.get('layer', ''))]
    queries = precise_queries + broad_queries

    # 每个API使用独立相似度检测器，方便多API并发。
    api_similarity_detector = SimilarityDetector()

    # 用于存储每个仓库的候选函数
    repo_candidates = {}  # {repo_name: [(function_data, quality_score), ...]}
    seen_files = set()
    skipped_duplicate_files = 0
    skipped_host_mismatch = 0
    stopped_after_enough_candidates = False

    for query_info in queries:
        query = query_info['query']
        layer = query_info['layer']
        is_broad_layer = is_broad_query_layer(layer)

        selectable_candidates = count_selectable_candidates(repo_candidates)
        if (
            globals().get('STOP_QUERY_WHEN_ENOUGH_CANDIDATES', True)
            and selectable_candidates >= globals().get('MIN_CANDIDATES_TO_STOP_QUERY', MAX_FUNCTIONS_PER_API)
        ):
            logger.info(
                f"\n  跳过后续查询层: 已有可选候选函数 {selectable_candidates} 个，"
                f"达到阈值 {globals().get('MIN_CANDIDATES_TO_STOP_QUERY', MAX_FUNCTIONS_PER_API)}"
            )
            stopped_after_enough_candidates = True
            break

        if is_broad_layer:
            if globals().get('RUN_BROAD_QUERIES_ONLY_AS_FALLBACK', True) and repo_candidates:
                logger.info(f"\n  跳过宽泛查询层: {layer}（精确查询已找到候选函数）")
                continue
            if should_skip_broad_query(query_info):
                logger.info(f"\n  跳过宽泛查询层: {layer}（关键词过宽: {query_info.get('keyword')}）")
                continue

        logger.info(f"\n  查询层: {layer}")
        logger.info(f"  查询: {query[:70]}...")

        max_results = get_max_results_for_query_layer(layer)
        search_results = search_github_code(query, max_results=max_results)
        logger.info(f"  找到 {len(search_results)} 个文件")

        # 优先处理高Star仓库
        search_results = prioritize_search_results(search_results)

        for item in search_results:
            repo_name = item.get('repository', {}).get('full_name', '')
            file_path = item.get('path', '')
            file_key = (repo_name, file_path)

            # 跳过非Python文件
            if not file_path.endswith('.py'):
                continue

            # 同一个API内避免跨查询层重复下载/处理同一文件
            if file_key in seen_files:
                skipped_duplicate_files += 1
                continue
            seen_files.add(file_key)

            # 跳过黑名单
            if repo_name in BLACKLIST_REPOS:
                logger.debug(f"  跳过黑名单仓库: {repo_name}")
                continue

            # 获取文件内容
            code = get_file_content(repo_name, file_path)
            if not code:
                continue

            # v3.1: 文件不包含目标host时，后续AST/质量/repo请求必然没有收益，提前跳过
            if globals().get('REQUIRE_HOST_IN_FILE', True) and not has_target_host(code, hosts):
                skipped_host_mismatch += 1
                continue

            # 检查是否自动生成
            if is_auto_generated_code(code, repo_name):
                logger.debug(f"  跳过自动生成代码: {repo_name}/{file_path}")
                continue

            # 检查代码相似度
            if api_similarity_detector.is_similar(code):
                logger.debug(f"  跳过相似代码: {repo_name}/{file_path}")
                continue

            # 检查质量（不修改代码）
            checks = check_code_quality(code)

            # 跳过数据库调用
            if checks['has_database_call']:
                logger.debug(f"  跳过数据库调用: {repo_name}/{file_path}")
                continue

            # 跳过无API调用的代码
            if not checks['has_api_call']:
                continue

            # 跳过有缩进问题的代码
            if checks['has_indentation_issue']:
                logger.debug(f"  跳过缩进问题: {repo_name}/{file_path}")
                continue

            # 跳过语法无效的代码
            if not checks['is_syntactically_valid']:
                logger.debug(f"  跳过语法错误: {repo_name}/{file_path}")
                continue

            # 提取函数
            functions = extract_functions_from_code(code, hosts)
            if not functions:
                continue

            # 只有真正提取到相关函数后才查询仓库信息
            repo_info = get_repo_info(repo_name)

            for func in functions:
                # 计算质量分
                quality_score = calculate_quality_score(func['code'], repo_info, checks)

                if quality_score < MIN_QUALITY_SCORE:
                    continue

                # 提取参数
                params = extract_api_parameters(func['code'])
                endpoint, doc_match_type = select_documented_endpoint(func['code'], api_data, hosts)

                function_data = {
                    'api_name': api_name,
                    'api_host': hosts[0] if hosts else '',
                    'function_name': func['name'],
                    'language': 'python',
                    'github_info': {
                        'repo': repo_name,
                        'file_path': file_path,
                        'file_url': f"https://github.com/{repo_name}/blob/main/{file_path}",
                        'stars': repo_info.get('stars', 0) if repo_info else 0,
                        'forks': repo_info.get('forks', 0) if repo_info else 0,
                        'watchers': repo_info.get('watchers', 0) if repo_info else 0,
                        'open_issues': repo_info.get('open_issues', 0) if repo_info else 0,
                        'created_at': repo_info.get('created_at', '') if repo_info else '',
                        'updated_at': repo_info.get('updated_at', '') if repo_info else '',
                        'description': repo_info.get('description', '') if repo_info else '',
                        'language': repo_info.get('language', '') if repo_info else '',
                        'license': repo_info.get('license', '') if repo_info else '',
                        'topics': repo_info.get('topics', []) if repo_info else [],
                        'query_layer': layer,
                    },
                    'code': {
                        'complete_function': func['code'],
                        'has_comments': checks['has_comments'],
                        'comment_lines': checks['comment_lines'],
                        'has_docstring': checks['has_docstring'],
                    },
                    'api_metadata': build_api_metadata(endpoint, params, doc_match_type),
                    'quality_metrics': {
                        'code_quality_score': quality_score,
                        'completeness_checks': checks,
                    },
                }

                # 添加到仓库候选列表
                if repo_name not in repo_candidates:
                    repo_candidates[repo_name] = []
                repo_candidates[repo_name].append((function_data, quality_score))

            # 添加到相似度检测器
            api_similarity_detector.add_code(code)

        time.sleep(QUERY_DELAY)

    if skipped_duplicate_files or skipped_host_mismatch:
        logger.info(
            f"  v3.1加速: 跳过重复文件 {skipped_duplicate_files} 个, "
            f"跳过不含目标host文件 {skipped_host_mismatch} 个"
        )
    if stopped_after_enough_candidates:
        logger.info("  v3.1加速: 候选函数充足，已提前停止该API的剩余查询层")

    # 从每个仓库选择最优的N个函数
    results = []
    for repo_name, candidates in repo_candidates.items():
        # 按质量分排序
        ranked = rank_functions_by_quality(candidates)

        # 选择前N个
        selected = ranked[:MAX_FUNCTIONS_PER_REPO]

        for func_data, score in selected:
            results.append(func_data)
            comment_info = f"注释{func_data['code']['comment_lines']}行" if func_data['code']['has_comments'] else "无注释"
            logger.info(f"    ✓ {func_data['function_name']} (质量: {score:.0f}, {comment_info}, 仓库: {repo_name})")

            # 达到API限制就停止
            if len(results) >= MAX_FUNCTIONS_PER_API:
                break

        if len(results) >= MAX_FUNCTIONS_PER_API:
            break

    logger.info(f"\n  总计: {len(results)} 个函数 (来自 {len(repo_candidates)} 个仓库)")
    return results

# ============================================================================
# 进度管理
# ============================================================================

def load_progress(progress_file):
    """加载进度"""
    if os.path.exists(progress_file):
        try:
            with open(progress_file, 'r', encoding='utf-8') as f:
                progress = json.load(f)
            # 兼容旧进度文件中的 all_results；v3.1 默认不再把结果写进进度文件。
            return progress.get('completed_apis', []), progress.get('all_results', [])
        except:
            return [], []
    return [], []

def save_progress(progress_file, completed_apis, all_results):
    """保存进度；v3.1 默认只保存轻量进度信息。"""
    progress_data = {
        'completed_apis': completed_apis,
        'last_update': datetime.now().isoformat(),
        'total_functions': len(all_results),
    }

    if globals().get('SAVE_FULL_RESULTS_IN_PROGRESS', False):
        progress_data['all_results'] = all_results

    os.makedirs(os.path.dirname(progress_file), exist_ok=True)

    with open(progress_file, 'w', encoding='utf-8') as f:
        json.dump(progress_data, f, ensure_ascii=False, indent=2)

def _result_dedupe_key(result: Dict) -> Tuple[str, str, str, str, str]:
    """结果去重键，兼容从final JSON和JSONL同时恢复。"""
    github_info = result.get('github_info', {})
    code = result.get('code', {})
    return (
        result.get('api_name', ''),
        github_info.get('repo', ''),
        github_info.get('file_path', ''),
        result.get('function_name', ''),
        code.get('complete_function', ''),
    )

def dedupe_results(results: List[Dict]) -> List[Dict]:
    """按函数来源和代码内容去重，保持原始顺序。"""
    seen = set()
    deduped = []
    for result in results:
        key = _result_dedupe_key(result)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(result)
    return deduped

def load_existing_results(output_dir: str, final_output: str) -> List[Dict]:
    """从final JSON和JSONL恢复已有结果，避免进度文件携带大体积结果。"""
    results = []
    jsonl_output = globals().get(
        'JSONL_OUTPUT',
        os.path.join(output_dir, f'all_functions.jsonl')
    )

    if os.path.exists(final_output):
        try:
            with open(final_output, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                results.extend(data)
        except Exception as e:
            logger.warning(f"读取已有最终结果失败: {e}")

    if os.path.exists(jsonl_output):
        try:
            with open(jsonl_output, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        results.append(json.loads(line))
        except Exception as e:
            logger.warning(f"读取JSONL结果失败: {e}")

    return dedupe_results(results)

def append_results_to_jsonl(results: List[Dict], output_dir: str):
    """实时追加结果到JSONL，降低中断时的数据丢失风险。"""
    if not results:
        return

    os.makedirs(output_dir, exist_ok=True)
    jsonl_output = globals().get(
        'JSONL_OUTPUT',
        os.path.join(output_dir, f'all_functions.jsonl')
    )

    with open(jsonl_output, 'a', encoding='utf-8') as f:
        for result in results:
            f.write(json.dumps(result, ensure_ascii=False) + '\n')

def save_api_results(api_name: str, results: List[Dict], output_dir: str):
    """保存单个API的结果"""
    if not results:
        return

    os.makedirs(output_dir, exist_ok=True)

    filename = f"{api_name.replace('/', '_')}_functions.json"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

# ============================================================================
# 统计报告
# ============================================================================

def generate_statistics_report(all_results: List[Dict]) -> Dict:
    """生成统计报告"""
    if not all_results:
        return {}

    total = len(all_results)

    # 质量统计
    quality_scores = [r['quality_metrics']['code_quality_score'] for r in all_results]
    avg_quality = sum(quality_scores) / total

    # 完整性统计
    syntax_valid = sum(1 for r in all_results if r['quality_metrics']['completeness_checks']['is_syntactically_valid'])
    has_api_call = sum(1 for r in all_results if r['quality_metrics']['completeness_checks']['has_api_call'])
    has_response = sum(1 for r in all_results if r['quality_metrics']['completeness_checks']['has_response_handling'])
    has_error = sum(1 for r in all_results if r['quality_metrics']['completeness_checks']['has_error_handling'])

    # 注释统计
    has_comments = sum(1 for r in all_results if r['code']['has_comments'])
    has_docstring = sum(1 for r in all_results if r['code']['has_docstring'])
    total_comment_lines = sum(r['code']['comment_lines'] for r in all_results)

    # API和仓库统计
    unique_apis = len(set(r['api_name'] for r in all_results))
    unique_repos = len(set(r['github_info']['repo'] for r in all_results))

    # 查询层统计
    from collections import Counter
    layer_counts = Counter(r['github_info']['query_layer'] for r in all_results)

    report = {
        'total_functions': total,
        'quality': {
            'average_score': round(avg_quality, 2),
            'min_score': round(min(quality_scores), 2),
            'max_score': round(max(quality_scores), 2),
        },
        'completeness': {
            'syntax_valid': f"{syntax_valid}/{total} ({100*syntax_valid/total:.1f}%)",
            'has_api_call': f"{has_api_call}/{total} ({100*has_api_call/total:.1f}%)",
            'has_response_handling': f"{has_response}/{total} ({100*has_response/total:.1f}%)",
            'has_error_handling': f"{has_error}/{total} ({100*has_error/total:.1f}%)",
        },
        'comments': {
            'has_comments': f"{has_comments}/{total} ({100*has_comments/total:.1f}%)",
            'has_docstring': f"{has_docstring}/{total} ({100*has_docstring/total:.1f}%)",
            'total_comment_lines': total_comment_lines,
            'avg_comment_lines': round(total_comment_lines / total, 2),
        },
        'coverage': {
            'unique_apis': unique_apis,
            'unique_repos': unique_repos,
            'avg_functions_per_api': round(total / unique_apis, 2),
            'avg_functions_per_repo': round(total / unique_repos, 2),
        },
        'query_layers': dict(layer_counts),
    }

    return report

def print_statistics_report(report: Dict):
    """打印统计报告"""
    print("\n" + "="*80)
    print("📊 爬取统计报告")
    print("="*80)

    print(f"\n📈 总体统计")
    print(f"总函数数: {report['total_functions']}")
    print(f"平均质量分: {report['quality']['average_score']}/100")
    print(f"质量分范围: {report['quality']['min_score']} - {report['quality']['max_score']}")

    print(f"\n✅ 完整性检查")
    print(f"语法有效: {report['completeness']['syntax_valid']}")
    print(f"有API调用: {report['completeness']['has_api_call']}")
    print(f"有响应处理: {report['completeness']['has_response_handling']}")
    print(f"有错误处理: {report['completeness']['has_error_handling']}")

    print(f"\n💬 注释统计")
    print(f"包含注释: {report['comments']['has_comments']}")
    print(f"包含docstring: {report['comments']['has_docstring']}")
    print(f"总注释行数: {report['comments']['total_comment_lines']}")
    print(f"平均注释行数: {report['comments']['avg_comment_lines']}")

    print(f"\n🌐 覆盖统计")
    print(f"API数量: {report['coverage']['unique_apis']}")
    print(f"仓库数量: {report['coverage']['unique_repos']}")
    print(f"平均每API函数数: {report['coverage']['avg_functions_per_api']}")
    print(f"平均每仓库函数数: {report['coverage']['avg_functions_per_repo']}")

    print(f"\n🔍 查询层分布")
    for layer, count in report['query_layers'].items():
        print(f"  {layer}: {count} 个函数")

    print("="*80)

# ============================================================================
# 主函数
# ============================================================================

def main():
    """主函数"""
    logger.info("="*80)
    logger.info("SCCGBench v3.1 - 增强版数据爬取 (并发加速版)")
    logger.info("="*80)

    # 加载RapidAPI元数据
    logger.info("\n加载RapidAPI元数据...")
    all_apis = load_rapidapi_metadata_by_category(get_rapidapi_metadata_dir())

    if not all_apis:
        logger.error("未找到API元数据，退出")
        return

    # 根据机器配置过滤API
    total_apis = len(all_apis)
    logger.info(f"\n总共加载 {total_apis} 个API")
    apis = filter_apis(all_apis)
    api_count = len(apis)
    logger.info(f"共 {api_count} 个 API 待处理")

    # 加载进度
    progress_file = get_progress_file()
    completed_apis, progress_results = load_progress(progress_file)
    if progress_results:
        all_results = dedupe_results(progress_results)
        logger.info("检测到旧版进度文件中的结果数据，已兼容加载")
    else:
        all_results = load_existing_results(get_output_dir(), get_final_output())
    completed_count = len(completed_apis)
    logger.info(f"\n已完成: {completed_count} 个API")
    logger.info(f"已爬取: {len(all_results)} 个函数")

    # 过滤已完成的API
    remaining_apis = [api for api in apis if api.get('api_name') not in completed_apis]
    remaining_count = len(remaining_apis)
    logger.info(f"剩余: {remaining_count} 个API\n")

    # 显示Token状态
    token_status = token_manager.get_status_summary()
    logger.info(f"Token状态: {token_status['active']}/{token_status['total']} 可用")

    # 开始爬取
    start_time = time.time()
    api_workers = max(1, int(globals().get('API_WORKERS', 1)))
    api_workers = min(api_workers, max(1, remaining_count))
    logger.info(f"API并发数: {api_workers}")

    def persist_api_result(api_name: str, results: List[Dict]):
        nonlocal all_results

        if results:
            append_results_to_jsonl(results, get_output_dir())
            all_results.extend(results)
            all_results = dedupe_results(all_results)
            save_api_results(api_name, results, get_output_dir())

        completed_apis.append(api_name)
        save_progress(progress_file, completed_apis, all_results)

        logger.info(f"✓ 本API爬取: {len(results) if results else 0} 个函数")
        logger.info(f"✓ 累计总数: {len(all_results)} 个函数")

    try:
        if api_workers == 1:
            for idx, api_data in enumerate(remaining_apis, 1):
                api_name = api_data.get('api_name', 'unknown')
                global_completed = completed_count + idx - 1
                global_remaining = api_count - global_completed

                logger.info(f"\n{'='*80}")
                logger.info(f"进度: 已处理 {global_completed}/{api_count} | 剩余 {global_remaining} 个API")
                logger.info(f"当前API ({idx}/{remaining_count}): {api_name}")
                logger.info(f"{'='*80}")

                token_status = token_manager.get_status_summary()
                logger.info(f"Token状态: {token_status['active']}可用 | {token_status['rate_limited']}限流 | 当前#{token_status['current_index']}")

                if len(all_results) >= TARGET_TOTAL_FUNCTIONS:
                    logger.info(f"\n✓ 已达到函数数上限: {TARGET_TOTAL_FUNCTIONS}")
                    break

                try:
                    persist_api_result(api_name, crawl_api_functions(api_data))
                except Exception as e:
                    logger.error(f"✗ 爬取失败: {api_name} - {e}")
                    continue

                time.sleep(API_DELAY)
        else:
            api_iter = iter(remaining_apis)
            submitted_count = 0
            completed_future_count = 0

            def submit_next(executor, future_to_api) -> bool:
                nonlocal submitted_count
                try:
                    api_data = next(api_iter)
                except StopIteration:
                    return False

                submitted_count += 1
                api_name = api_data.get('api_name', 'unknown')
                logger.info(f"提交API任务 ({submitted_count}/{remaining_count}): {api_name}")
                future_to_api[executor.submit(crawl_api_functions, api_data)] = api_data
                return True

            with ThreadPoolExecutor(max_workers=api_workers) as executor:
                future_to_api = {}
                for _ in range(api_workers):
                    submit_next(executor, future_to_api)

                while future_to_api:
                    if len(all_results) >= TARGET_TOTAL_FUNCTIONS:
                        logger.info(f"\n✓ 已达到函数数上限: {TARGET_TOTAL_FUNCTIONS}，停止提交新任务")
                        break

                    done_future = None
                    for done_future in as_completed(list(future_to_api.keys())):
                        break

                    api_data = future_to_api.pop(done_future)
                    api_name = api_data.get('api_name', 'unknown')
                    completed_future_count += 1

                    token_status = token_manager.get_status_summary()
                    logger.info(f"\n{'='*80}")
                    logger.info(
                        f"完成API任务 {completed_future_count}/{remaining_count}: {api_name} | "
                        f"当前函数数 {len(all_results)} | Token {token_status['active']}可用/{token_status['rate_limited']}限流"
                    )
                    logger.info(f"{'='*80}")

                    try:
                        results = done_future.result()
                        persist_api_result(api_name, results)
                    except Exception as e:
                        logger.error(f"✗ 爬取失败: {api_name} - {e}")
                        save_progress(progress_file, completed_apis, all_results)

                    if len(all_results) < TARGET_TOTAL_FUNCTIONS:
                        submit_next(executor, future_to_api)

                for pending_future in future_to_api:
                    pending_future.cancel()

    except KeyboardInterrupt:
        logger.info("\n\n⚠️  用户中断，保存进度...")
        save_progress(progress_file, completed_apis, all_results)

    # 保存最终结果
    logger.info("\n保存最终结果...")
    final_output = get_final_output()
    all_results = dedupe_results(all_results)
    with open(final_output, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    # 生成统计报告
    report = generate_statistics_report(all_results)
    report_file = os.path.join(get_output_dir(), f'statistics_report.json')
    if report:
        print_statistics_report(report)

        # 保存统计报告
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    else:
        logger.info("暂无函数结果，跳过统计报告生成")

    elapsed_time = time.time() - start_time
    logger.info(f"\n✓ 爬取完成！")
    logger.info(f"总耗时: {elapsed_time/3600:.2f} 小时")
    logger.info(f"最终结果: {final_output}")
    if report:
        logger.info(f"统计报告: {report_file}")

    # 显示最终Token使用统计
    token_status = token_manager.get_status_summary()
    logger.info(f"\nToken最终状态:")
    logger.info(f"  总计: {token_status['total']}")
    logger.info(f"  可用: {token_status['active']}")
    logger.info(f"  限流: {token_status['rate_limited']}")
    logger.info(f"  错误: {token_status['error']}")

if __name__ == '__main__':
    main()
