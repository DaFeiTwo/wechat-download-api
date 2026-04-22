#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
单篇文章下载路由 - 下载微信公众号文章并保存到数据库
"""

import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from utils.article_fetcher import fetch_article_content
from utils.content_processor import process_article_content
from utils.helpers import (
    extract_article_info,
    get_client_ip,
    get_unavailable_reason,
    has_article_content,
    is_article_unavailable,
    parse_article_url,
)
from utils import rss_store

logger = logging.getLogger(__name__)

router = APIRouter()


class DownloadRequest(BaseModel):
    """下载请求"""
    url: str = Field(..., description="微信文章链接，如 https://mp.weixin.qq.com/s/xxxxx")


class DownloadResponse(BaseModel):
    """下载响应"""
    success: bool = Field(..., description="是否成功")
    article_id: Optional[int] = Field(None, description="文章数据库 ID")
    title: Optional[str] = Field(None, description="文章标题")
    message: Optional[str] = Field(None, description="操作结果描述")
    error: Optional[str] = Field(None, description="错误信息，成功时为 null")


@router.post("/article/download", response_model=DownloadResponse, summary="下载并保存微信文章")
async def download_article(req: DownloadRequest, request: Request):
    """
    下载微信公众号文章并保存到数据库。

    **请求体参数：**
    - **url** (必填): 微信文章链接，支持 `https://mp.weixin.qq.com/s/xxxxx` 格式

    **返回字段：**
    - `success`: 是否成功
    - `article_id`: 文章数据库 ID（成功时返回）
    - `title`: 文章标题（成功时返回）
    - `message`: 操作结果描述
    - `error`: 错误信息（失败时返回）
    """
    client_ip = get_client_ip(request)
    url = req.url.strip()

    # 1. 验证 URL 格式
    if "mp.weixin.qq.com" not in url:
        return DownloadResponse(
            success=False,
            error="URL 格式无效，仅支持微信公众号文章链接（mp.weixin.qq.com）",
        )

    try:
        logger.info("[Download] request from %s: %s", client_ip, url[:80])

        # 2. 解析 URL 参数
        params = parse_article_url(url)

        # 3. 获取文章 HTML
        html = await fetch_article_content(url)
        if not html:
            return DownloadResponse(
                success=False,
                error="无法获取文章内容，请检查链接是否有效或稍后重试",
            )

        # 4. 检查文章可用性
        if is_article_unavailable(html):
            reason = get_unavailable_reason(html) or "文章不可用"
            return DownloadResponse(success=False, error=f"文章不可用：{reason}")

        if not has_article_content(html):
            return DownloadResponse(
                success=False,
                error="无法获取文章内容。可能原因：文章被删除、访问受限或需要验证。",
            )

        # 5. 提取文章元数据
        article_info = extract_article_info(html, params)

        # 6. 确定 fakeid
        fakeid = _resolve_fakeid(params, html)

        # 7. 处理文章内容（图片代理等）
        site_url = os.getenv("SITE_URL", "").rstrip("/")
        processed = process_article_content(html, proxy_base_url=site_url)

        # 8. 提取封面图
        cover = _extract_cover(html)

        # 9. 提取摘要
        digest = _extract_digest(html, processed.get("plain_content", ""))

        # 10. 构建文章数据
        link = url
        article_data = {
            "aid": "",
            "title": article_info.get("title", ""),
            "link": link,
            "digest": digest,
            "cover": cover,
            "author": article_info.get("author", ""),
            "content": processed.get("content", ""),
            "plain_content": processed.get("plain_content", ""),
            "publish_time": article_info.get("publish_time", 0),
        }

        # 11. 保存文章
        rss_store.save_articles(fakeid, [article_data])

        # 12. 查询保存后的文章记录
        saved = rss_store.get_article_by_link(fakeid, link)
        if not saved:
            return DownloadResponse(
                success=False,
                error="文章保存失败，请稍后重试",
            )

        title = article_data["title"] or "无标题"
        logger.info("[Download] saved article_id=%s title=%s", saved["id"], title)

        return DownloadResponse(
            success=True,
            article_id=saved["id"],
            title=title,
            message="文章下载保存成功",
        )

    except Exception as e:
        error_str = str(e)
        logger.exception("[Download] error: %s", error_str[:200])
        if "timeout" in error_str.lower():
            return DownloadResponse(success=False, error="请求超时，请稍后重试")
        return DownloadResponse(success=False, error=f"处理请求时发生错误: {error_str}")


def _resolve_fakeid(params: Optional[dict], html: str) -> str:
    """
    确定 fakeid：优先从 URL 参数提取 __biz，其次从 HTML 正则匹配，兜底 __standalone__
    """
    # 优先从 URL 参数
    if params and params.get("__biz"):
        return params["__biz"]

    # 从 HTML 中正则匹配 var __biz = "..."
    biz_match = re.search(r'var\s+__biz\s*=\s*"([^"]+)"', html)
    if biz_match:
        return biz_match.group(1)

    # 兜底
    return "__standalone__"


def _extract_cover(html: str) -> str:
    """从 og:image meta 标签提取封面图 URL"""
    match = re.search(
        r'<meta\s+property="og:image"\s+content="([^"]+)"', html, re.IGNORECASE
    )
    if match:
        return match.group(1)
    return ""


def _extract_digest(html: str, plain_content: str) -> str:
    """提取摘要：优先 meta description，其次 plain_content 截取前 120 字符"""
    match = re.search(
        r'<meta\s+name="description"\s+content="([^"]*)"', html, re.IGNORECASE
    )
    if match and match.group(1).strip():
        return match.group(1).strip()

    if plain_content:
        return plain_content[:120]

    return ""
