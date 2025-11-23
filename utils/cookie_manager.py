"""
统一的Cookie管理器
整合JSON文件和环境变量Cookie的检测、加载和管理功能
"""

import os
import json
from dataclasses import dataclass
from typing import List, Dict, Optional
from utils.paths import cookies_dir
from utils.cookie_handler import auto_convert_to_playwright
from utils.common import clean_env_value

@dataclass
class CookieSource:
    """Cookie来源的统一表示"""
    type: str  # "file" | "env_var"
    identifier: str  # filename or "USER_COOKIE_1"
    display_name: str  # 显示名称

    def __str__(self):
        return f"{self.type}:{self.identifier}"


class CookieManager:
    """
    统一的Cookie管理器
    负责检测、加载和缓存所有来源的Cookie数据
    """

    def __init__(self, logger=None):
        self.logger = logger
        self._detected_sources: Optional[List[CookieSource]] = None
        self._cookie_cache: Dict[str, List[Dict]] = {}

    def detect_all_sources(self) -> List[CookieSource]:
        """
        检测所有可用的Cookie来源（JSON文件 + 环境变量）
        结果会被缓存，避免重复扫描
        """
        if self._detected_sources is not None:
            return self._detected_sources

        sources = []

        # 1. 扫描Cookies目录中的JSON文件
        try:
            cookie_path = cookies_dir()
            if os.path.isdir(cookie_path):
                cookie_files = [f for f in os.listdir(cookie_path) if f.lower().endswith('.json')]

                for cookie_file in cookie_files:
                    source = CookieSource(
                        type="file",
                        identifier=cookie_file,
                        display_name=cookie_file
                    )
                    sources.append(source)

                if cookie_files and self.logger:
                    self.logger.info(f"发现 {len(cookie_files)} 个 Cookie 文件")
                elif self.logger:
                    self.logger.info(f"在 {cookie_path} 目录下未找到任何格式的 Cookie 文件")
            else:
                if self.logger:
                    self.logger.error(f"Cookie 目录不存在: {cookie_path}")

        except Exception as e:
            if self.logger:
                self.logger.error(f"扫描 Cookie 目录时出错: {e}")

        # 2. 扫描USER_COOKIE环境变量
        cookie_index = 1
        env_cookie_count = 0

        while True:
            env_var_name = f"USER_COOKIE_{cookie_index}"
            env_value = clean_env_value(os.getenv(env_var_name))

            if not env_value:
                if cookie_index == 1 and self.logger:
                    self.logger.info(f"未检测到任何 USER_COOKIE 环境变量")
                break

            source = CookieSource(
                type="env_var",
                identifier=env_var_name,
                display_name=env_var_name
            )
            sources.append(source)

            env_cookie_count += 1
            cookie_index += 1

        if env_cookie_count > 0 and self.logger:
            self.logger.info(f"发现 {env_cookie_count} 个 Cookie 环境变量")

        # 缓存结果
        self._detected_sources = sources
        return sources

    def load_cookies(self, source: CookieSource) -> List[Dict]:
        """
        从指定来源加载Cookie数据

        Args:
            source: Cookie来源对象

        Returns:
            Playwright兼容的cookie列表
        """
        cache_key = str(source)

        # 检查缓存
        if cache_key in self._cookie_cache:
            if self.logger:
                self.logger.debug(f"从缓存加载 Cookie: {source.display_name}")
            return self._cookie_cache[cache_key]

        cookies = []

        try:
            if source.type == "file":
                cookies = self._load_from_file(source.identifier)
            elif source.type == "env_var":
                cookies = self._load_from_env(source.identifier)
            else:
                if self.logger:
                    self.logger.error(f"未知的 Cookie 来源类型: {source.type}")
                return []

            # 缓存结果
            self._cookie_cache[cache_key] = cookies

            if self.logger:
                self.logger.info(f"从 {source.display_name} 加载了 {len(cookies)} 个 Cookie 数据")

        except Exception as e:
            if self.logger:
                self.logger.error(f"从 {source.display_name} 加载 Cookie 时出错: {e}")
            return []

        return cookies

    def _load_from_file(self, filename: str) -> List[Dict]:
        """从文件加载 Cookie，自动识别 JSON 或 KV 格式"""
        cookie_path = cookies_dir() / filename

        if not os.path.exists(cookie_path):
            raise FileNotFoundError(f"Cookie 文件不存在: {cookie_path}")

        with open(cookie_path, 'r', encoding='utf-8') as f:
            file_content = f.read().strip()

        # 尝试解析为 JSON
        try:
            cookies_from_file = json.loads(file_content)
            # JSON 解析成功，使用自动转换函数
            return auto_convert_to_playwright(
                cookies_from_file,
                default_domain=".google.com",
                logger=self.logger
            )
        except json.JSONDecodeError:
            # JSON 解析失败，当作 KV 格式处理
            if self.logger:
                self.logger.info(f"文件 {filename} 不是有效的 JSON 格式，尝试作为 KV 格式解析")
            return auto_convert_to_playwright(
                file_content,
                default_domain=".google.com",
                logger=self.logger
            )

    def _load_from_env(self, env_var_name: str) -> List[Dict]:
        """从环境变量加载 Cookie，自动识别 JSON 或 KV 格式"""
        env_value = clean_env_value(os.getenv(env_var_name))

        if not env_value:
            raise ValueError(f"环境变量 {env_var_name} 不存在或为空")

        # 尝试解析为 JSON
        try:
            cookies_from_env = json.loads(env_value)
            # JSON 解析成功，使用自动转换函数
            return auto_convert_to_playwright(
                cookies_from_env,
                default_domain=".google.com",
                logger=self.logger
            )
        except json.JSONDecodeError:
            # JSON 解析失败，当作 KV 格式处理
            if self.logger:
                self.logger.debug(f"环境变量 {env_var_name} 不是有效的 JSON 格式，作为 KV 格式解析")
            return auto_convert_to_playwright(
                env_value,
                default_domain=".google.com",
                logger=self.logger
            )
