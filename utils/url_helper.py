"""
URL处理辅助函数

提供URL解析和路径提取功能，用于导航验证中的域名无关匹配。
"""

from urllib.parse import urlparse


def extract_url_path(url: str) -> str:
    """
    提取URL的路径和查询参数部分，忽略协议和域名差异

    用于验证导航是否到达正确页面，允许域名重定向。

    Args:
        url: 完整URL字符串

    Returns:
        路径+查询参数+片段（例如："/apps/drive/123?param=value#section"）
        如果URL为空或无效，返回空字符串

    Examples:
        >>> extract_url_path("https://ai.studio/apps/drive/123?param=value")
        '/apps/drive/123?param=value'

        >>> extract_url_path("https://aistudio.google.com/apps/drive/123")
        '/apps/drive/123'

        >>> extract_url_path("https://example.com/path")
        '/path'
    """
    if not url:
        return ""

    try:
        parsed = urlparse(url)
        result = parsed.path
        if parsed.query:
            result += '?' + parsed.query
        if parsed.fragment:
            result += '#' + parsed.fragment
        return result
    except Exception:
        # 如果URL格式无效，返回空字符串
        return ""


def mask_path_for_logging(path: str) -> str:
    """
    对路径进行脱敏处理，用于日志输出

    脱敏规则：
    1. 对于 /apps/drive/XXXXXXXXXX 路径，保留头4位和尾4位，中间用***代替
    2. 如果不是 /apps/drive/XXXXXXXXXX 路径，返回完整路径

    Args:
        path: URL路径字符串

    Returns:
        脱敏后的路径字符串

    Examples:
        >>> mask_path_for_logging("/apps/drive/abcdef123456")
        '/apps/drive/abcd***3456'

        >>> mask_path_for_logging("/apps/drive/xyz789")
        '/apps/drive/xyz789'

        >>> mask_path_for_logging("/other/path")
        '/other/path'
    """
    if not path:
        return ""

    # 检查是否为 /apps/drive/ 路径
    if path.startswith('/apps/drive/'):
        # 提取路径中的ID部分
        path_parts = path.split('/')
        if len(path_parts) >= 4:  # ['', 'apps', 'drive', 'ID']
            drive_id = path_parts[3]

            # 如果ID长度大于8，则进行脱敏处理
            if len(drive_id) > 8:
                # 使用与URL脱敏相同的格式
                masked_id = f"{drive_id[:4]}***{drive_id[-4:]}"
                # 重新构建路径
                masked_parts = path_parts[:3] + [masked_id] + path_parts[4:]
                return '/'.join(masked_parts)

    # 如果不符合脱敏条件，返回原始路径
    return path


def mask_url_for_logging(url: str) -> str:
    """
    对URL进行脱敏处理，用于日志输出

    脱敏规则：
    1. 对于 /apps/drive/XXXXXXXXXX 路径，保留头4位和尾4位，中间用***代替
    2. 如果不是 /apps/drive/XXXXXXXXXX 路径，返回完整URL

    Args:
        url: 完整URL字符串

    Returns:
        脱敏后的URL字符串

    Examples:
        >>> mask_url_for_logging("https://ai.studio/apps/drive/abcdef123456")
        'https://ai.studio/apps/drive/abcd***3456'

        >>> mask_url_for_logging("https://aistudio.google.com/apps/drive/xyz789")
        'https://aistudio.google.com/apps/drive/xyz789'

        >>> mask_url_for_logging("https://example.com/other/path")
        'https://example.com/other/path'
    """
    if not url:
        return ""

    try:
        parsed = urlparse(url)

        # 检查是否为 /apps/drive/ 路径
        if parsed.path.startswith('/apps/drive/'):
            # 提取路径中的ID部分
            path_parts = parsed.path.split('/')
            if len(path_parts) >= 4:  # ['', 'apps', 'drive', 'ID']
                drive_id = path_parts[3]

                # 如果ID长度大于8，则进行脱敏处理
                if len(drive_id) > 8:
                    masked_id = f"{drive_id[:4]}***{drive_id[-4:]}"
                    # 重新构建路径
                    masked_parts = path_parts[:3] + [masked_id] + path_parts[4:]
                    masked_path = '/'.join(masked_parts)

                    # 重新构建URL
                    result = f"{parsed.scheme}://{parsed.netloc}{masked_path}"
                    if parsed.query:
                        result += '?' + parsed.query
                    if parsed.fragment:
                        result += '#' + parsed.fragment
                    return result

        # 如果不符合脱敏条件，返回原始URL
        return url

    except Exception:
        # 如果URL解析失败，返回原始URL
        return url
