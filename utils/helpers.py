#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
辅助函数模块
提供各种工具函数
"""

import re
from typing import Dict, Optional
from urllib.parse import urlparse, parse_qs

def html_to_text(html: str) -> str:
    """将 HTML 转为可读纯文本"""
    import html as html_module
    text = re.sub(r'<br\s*/?\s*>', '\n', html, flags=re.IGNORECASE)
    text = re.sub(r'</(?:p|div|section|h[1-6]|tr|li|blockquote)>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<hr[^>]*>', '\n---\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = html_module.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def parse_article_url(url: str) -> Optional[Dict[str, str]]:
    """
    解析微信文章URL，提取参数
    
    Args:
        url: 微信文章URL
        
    Returns:
        包含__biz, mid, idx, sn的字典，如果解析失败返回None
    """
    try:
        # 确保是微信文章URL
        if not url or 'mp.weixin.qq.com/s' not in url:
            return None
        
        parsed = urlparse(str(url))  # 确保url是字符串
        params = parse_qs(parsed.query)
        
        __biz = params.get('__biz', [''])[0]
        mid = params.get('mid', [''])[0]
        idx = params.get('idx', [''])[0]
        sn = params.get('sn', [''])[0]
        
        # 必须有这4个参数才返回
        if not all([__biz, mid, idx, sn]):
            return None
        
        return {
            '__biz': __biz,
            'mid': mid,
            'idx': idx,
            'sn': sn
        }
    except Exception:
        return None

def is_image_text_message(html: str) -> bool:
    """检测是否为图文消息（item_show_type=8，类似小红书多图+文字）"""
    m = re.search(r"window\.item_show_type\s*=\s*'(\d+)'", html)
    return m is not None and m.group(1) == '8'


def _extract_image_text_content(html: str) -> Dict:
    """
    提取图文消息的内容（item_show_type=8）

    图文消息的结构与普通文章完全不同：
    - 图片在 picture_page_info_list 的 JsDecode() 中
    - 文字在 meta description 或 content_desc 中
    - 没有 #js_content div
    """
    import html as html_module

    # 提取图片 URL（从 picture_page_info_list 中的 cdn_url）
    # 页面中有两种格式:
    #   1. picture_page_info_list: [ { cdn_url: JsDecode('...'), ... } ]  (带JsDecode)
    #   2. picture_page_info_list = [ { width:..., height:..., cdn_url: '...' } ]  (简单格式)
    # 每个 item 中第一个 cdn_url 是主图，watermark_info 内的是水印，需要跳过
    images = []

    # 优先使用简单格式（第二种），更易解析且包含所有图片
    simple_list_pos = html.find('picture_page_info_list = [')
    if simple_list_pos >= 0:
        bracket_start = html.find('[', simple_list_pos)
        depth = 0
        end = bracket_start
        for end in range(bracket_start, min(bracket_start + 20000, len(html))):
            if html[end] == '[':
                depth += 1
            elif html[end] == ']':
                depth -= 1
                if depth == 0:
                    break
        block = html[bracket_start:end + 1]
        # 按顶层 { 分割，每个 item 取第一个 cdn_url（主图）
        items = re.split(r'\n\s{4,10}\{', block)
        for item in items:
            m = re.search(r"cdn_url:\s*'([^']+)'", item)
            if m:
                url = m.group(1)
                if url not in images and ('mmbiz.qpic.cn' in url or 'mmbiz.qlogo.cn' in url):
                    images.append(url)

    # 降级: 使用 JsDecode 格式
    if not images:
        jsdecode_list_match = re.search(
            r'picture_page_info_list:\s*\[', html
        )
        if jsdecode_list_match:
            block_start = jsdecode_list_match.end() - 1
            depth = 0
            end = block_start
            for end in range(block_start, min(block_start + 20000, len(html))):
                if html[end] == '[':
                    depth += 1
                elif html[end] == ']':
                    depth -= 1
                    if depth == 0:
                        break
            block = html[block_start:end + 1]
            # 按顶层 { 分割
            items = re.split(r'\n\s{10,30}\{(?=\s*\n\s*cdn_url)', block)
            for item in items:
                m = re.search(r"cdn_url:\s*JsDecode\('([^']+)'\)", item)
                if m:
                    url = m.group(1).replace('\\x26amp;', '&').replace('\\x26', '&')
                    if url not in images and ('mmbiz.qpic.cn' in url or 'mmbiz.qlogo.cn' in url):
                        images.append(url)

    # 提取文字描述
    desc = ''
    # 方法1: meta description
    desc_match = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', html)
    if desc_match:
        desc = desc_match.group(1)
        # 处理 \x26 编码（微信的双重编码：\x26lt; -> &lt; -> <）
        desc = re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), desc)
        desc = html_module.unescape(desc)
        # 二次 unescape 处理双重编码
        desc = html_module.unescape(desc)
        # 清理 HTML 标签残留
        desc = re.sub(r'<[^>]+>', '', desc)
        desc = desc.replace('\\x0a', '\n').replace('\\n', '\n')

    # 方法2: content_desc
    if not desc:
        desc_match2 = re.search(r"content_desc:\s*JsDecode\('([^']*)'\)", html)
        if desc_match2:
            desc = desc_match2.group(1)
            desc = html_module.unescape(desc)

    # 构建 HTML 内容：竖向画廊 + 文字（RSS 兼容）
    html_parts = []

    # 竖向画廊：每张图限宽，紧凑排列，兼容主流 RSS 阅读器
    if images:
        gallery_imgs = []
        for i, img_url in enumerate(images):
            gallery_imgs.append(
                f'<p style="text-align:center;margin:0 0 6px">'
                f'<img src="{img_url}" data-src="{img_url}" '
                f'style="max-width:480px;width:100%;height:auto;border-radius:4px" />'
                f'</p>'
            )
        gallery_imgs.append(
            f'<p style="text-align:center;color:#999;font-size:12px;margin:4px 0 0">'
            f'{len(images)} images'
            f'</p>'
        )
        html_parts.append('\n'.join(gallery_imgs))

    # 文字描述区域
    if desc:
        text_lines = []
        for line in desc.split('\n'):
            line = line.strip()
            if line:
                text_lines.append(
                    f'<p style="margin:0 0 8px;line-height:1.8;font-size:15px;color:#333">{line}</p>'
                )
        html_parts.append('\n'.join(text_lines))

    content = '\n'.join(html_parts)
    plain_content = desc if desc else ''

    return {
        'content': content,
        'plain_content': plain_content,
        'images': images,
    }


def extract_article_info(html: str, params: Optional[Dict] = None) -> Dict:
    """
    从HTML中提取文章信息

    Args:
        html: 文章HTML内容
        params: URL参数（可选，用于返回__biz等信息）

    Returns:
        文章信息字典
    """

    title = ''
    # 图文消息的标题通常在 window.msg_title 中
    title_match = (
        re.search(r'<h1[^>]*class=[^>]*rich_media_title[^>]*>([\s\S]*?)</h1>', html, re.IGNORECASE) or
        re.search(r'<h2[^>]*class=[^>]*rich_media_title[^>]*>([\s\S]*?)</h2>', html, re.IGNORECASE) or
        re.search(r"var\s+msg_title\s*=\s*'([^']+)'\.html\(false\)", html) or
        re.search(r"window\.msg_title\s*=\s*window\.title\s*=\s*'([^']*)'", html) or
        re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
    )

    if title_match:
        title = title_match.group(1)
        title = re.sub(r'<[^>]+>', '', title)
        title = title.replace('&quot;', '"').replace('&amp;', '&').strip()

    author = ''
    author_match = (
        re.search(r'<a[^>]*id="js_name"[^>]*>([\s\S]*?)</a>', html, re.IGNORECASE) or
        re.search(r'var\s+nickname\s*=\s*"([^"]+)"', html) or
        re.search(r'<meta\s+property="og:article:author"\s+content="([^"]+)"', html) or
        re.search(r'<a[^>]*class=[^>]*rich_media_meta_nickname[^>]*>([^<]+)</a>', html, re.IGNORECASE)
    )

    if author_match:
        author = author_match.group(1)
        author = re.sub(r'<[^>]+>', '', author).strip()

    publish_time = 0
    time_match = (
        re.search(r'var\s+publish_time\s*=\s*"(\d+)"', html) or
        re.search(r'var\s+ct\s*=\s*"(\d+)"', html) or
        re.search(r"var\s+ct\s*=\s*'(\d+)'", html) or
        re.search(r'<em[^>]*id="publish_time"[^>]*>([^<]+)</em>', html)
    )

    if time_match:
        try:
            publish_time = int(time_match.group(1))
        except (ValueError, TypeError):
            pass

    # 检测是否为图文消息（item_show_type=8）
    if is_image_text_message(html):
        img_text_data = _extract_image_text_content(html)
        content = img_text_data['content']
        images = img_text_data['images']
        plain_content = img_text_data['plain_content']
    else:
        content = ''
        images = []

        # 方法1: 匹配 id="js_content"
        content_match = re.search(r'<div[^>]*id="js_content"[^>]*>([\s\S]*?)<script[^>]*>[\s\S]*?</script>', html, re.IGNORECASE)

        if not content_match:
            # 方法2: 匹配 class包含rich_media_content
            content_match = re.search(r'<div[^>]*class="[^"]*rich_media_content[^"]*"[^>]*>([\s\S]*?)</div>', html, re.IGNORECASE)

        if content_match and content_match.group(1):
            content = content_match.group(1).strip()
        else:
            # 方法3: 手动截取
            js_content_pos = html.find('id="js_content"')
            if js_content_pos > 0:
                start = html.find('>', js_content_pos) + 1
                script_pos = html.find('<script', start)
                if script_pos > start:
                    content = html[start:script_pos].strip()
        if content:
            # 提取data-src属性
            img_regex = re.compile(r'<img[^>]+data-src="([^"]+)"')
            for img_match in img_regex.finditer(content):
                img_url = img_match.group(1)
                if img_url not in images:
                    images.append(img_url)

            # 提取src属性
            img_regex2 = re.compile(r'<img[^>]+src="([^"]+)"')
            for img_match in img_regex2.finditer(content):
                img_url = img_match.group(1)
                if not img_url.startswith('data:') and img_url not in images:
                    images.append(img_url)

        content = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', content, flags=re.IGNORECASE)
        plain_content = html_to_text(content) if content else ''

    __biz = params.get('__biz', 'unknown') if params else 'unknown'
    publish_time_str = ''
    if publish_time > 0:
        from datetime import datetime
        dt = datetime.fromtimestamp(publish_time)
        publish_time_str = dt.strftime('%Y-%m-%d %H:%M:%S')

    return {
        'title': title,
        'content': content,
        'plain_content': plain_content,
        'images': images,
        'author': author,
        'publish_time': publish_time,
        'publish_time_str': publish_time_str,
        '__biz': __biz
    }

def has_article_content(html: str) -> bool:
    """
    Check whether the fetched HTML likely contains article content.
    Different WeChat account types use different content containers.
    """
    content_markers = [
        "js_content",
        "rich_media_content",
        "rich_media_area_primary",
        "page-content",
        "page_content",
    ]
    if any(marker in html for marker in content_markers):
        return True
    return is_image_text_message(html)


def get_client_ip(request) -> str:
    """
    Extract real client IP from request, respecting reverse proxy headers.
    Priority: X-Forwarded-For > X-Real-IP > request.client.host
    """
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip", "")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


def is_article_deleted(html: str) -> bool:
    """检查文章是否被删除"""
    return '已删除' in html or 'deleted' in html.lower()

def is_need_verification(html: str) -> bool:
    """检查是否需要验证"""
    return ('verify' in html.lower() or
            '验证' in html or
            '环境异常' in html)

def is_login_required(html: str) -> bool:
    """检查是否需要登录"""
    return '请登录' in html or 'login' in html.lower()

def time_str_to_microseconds(time_str: str) -> int:
    """
    将时间字符串转换为微秒
    
    支持格式：
    - "5s" -> 5秒
    - "1m30s" -> 1分30秒
    - "1h30m" -> 1小时30分
    - "00:01:30" -> 1分30秒
    - 直接数字 -> 微秒
    """
    if isinstance(time_str, int):
        return time_str
    
    # 尝试解析为整数（已经是微秒）
    try:
        return int(time_str)
    except ValueError:
        pass
    
    # 解析时间字符串
    total_seconds = 0
    
    # 格式：HH:MM:SS 或 MM:SS
    if ':' in time_str:
        parts = time_str.split(':')
        if len(parts) == 3:
            total_seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            total_seconds = int(parts[0]) * 60 + int(parts[1])
    else:
        # 格式：1h30m45s
        hours = re.search(r'(\d+)h', time_str)
        minutes = re.search(r'(\d+)m', time_str)
        seconds = re.search(r'(\d+)s', time_str)
        
        if hours:
            total_seconds += int(hours.group(1)) * 3600
        if minutes:
            total_seconds += int(minutes.group(1)) * 60
        if seconds:
            total_seconds += int(seconds.group(1))
    
    return total_seconds * 1000000  # 转换为微秒


