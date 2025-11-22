import os
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def project_root() -> Path:
    """
    返回代码仓库根目录，使调用者能够构建不依赖当前工作目录的绝对路径。
    """
    env_root = os.getenv("CAMOUFOX_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "cookies").exists():
            return parent

    # 如果标记目录缺失，则回退到原始行为
    return current.parents[min(2, len(current.parents) - 1)]


def logs_dir() -> Path:
    """存储日志文件和截图的根级目录。"""
    return project_root() / "logs"


def cookies_dir() -> Path:
    """存储持久化Cookie JSON文件的根级目录。"""
    return project_root() / "cookies"
