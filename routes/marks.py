#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
文章标记路由(收藏 / 待看)

Mark_API:负责文章收藏(Favorite)与待看(Watchlist)两类互相独立的标记操作。
挂载方式:在 app.py 中通过 `app.include_router(marks.router, prefix="/api")`
挂载,保持与现有 `rss.router` 一致的风格(本文件内不设置 prefix)。

端点(由后续子任务 2.2 / 2.3 实现):
- GET    /rss/article/{article_id}/marks      查询两类标记状态
- PUT    /rss/article/{article_id}/favorite   置为已收藏(幂等)
- DELETE /rss/article/{article_id}/favorite   取消收藏(幂等)
- PUT    /rss/article/{article_id}/watchlist  置为已加入待看(幂等)
- DELETE /rss/article/{article_id}/watchlist  取消待看(幂等)
- GET    /rss/marks/favorites                 分页列出已收藏
- GET    /rss/marks/watchlist                 分页列出已加入待看
"""

import logging
import math
import os

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from utils import rss_store
from utils.image_proxy import proxy_image_url

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Pydantic models ──────────────────────────────────────

class MarksData(BaseModel):
    """单篇文章的两类标记状态。"""
    favorite: bool = Field(..., description="是否已收藏")
    watchlist: bool = Field(..., description="是否已加入待看")


class MarksResponse(BaseModel):
    """GET /rss/article/{article_id}/marks 响应体。"""
    success: bool
    data: MarksData


class MarkActionResponse(BaseModel):
    """PUT / DELETE 单条标记操作的响应体(幂等,仅回执成功标志)。"""
    success: bool


class MarksListResponse(BaseModel):
    """
    GET /rss/marks/favorites 与 GET /rss/marks/watchlist 的分页响应体。

    `data` 中每项的字段集合与 /api/rss/articles 保持一致
    (id / title / link / digest / cover / author / publish_time /
     nickname / head_img / fakeid / read_at / is_favorite / is_watchlist),
    以便前端复用同一渲染函数。
    """
    success: bool
    data: list = []
    total: int = 0
    page: int = 1
    page_size: int = 20
    total_pages: int = 0


# ── 单条标记端点 (GET / PUT / DELETE) ────────────────────

# 说明:404 路径统一 raise HTTPException(status_code=404, detail="文章不存在"),
# 由 app.py 任务 2.4 中配置的全局 exception_handler 转换为
# {success: false, error: "文章不存在"} 结构,本文件内只负责触发。

_ARTICLE_NOT_FOUND = "文章不存在"


@router.get(
    "/rss/article/{article_id}/marks",
    response_model=MarksResponse,
    summary="查询文章标记状态",
)
async def get_article_marks(
    article_id: int = Path(..., ge=1, description="文章 ID (articles.id,>=1)"),
):
    """
    查询指定文章的收藏 / 待看标记状态。

    - 200: `{success: true, data: {favorite, watchlist}}`
    - 404: 文章不存在
    """
    if not rss_store.article_exists(article_id):
        raise HTTPException(status_code=404, detail=_ARTICLE_NOT_FOUND)

    marks = rss_store.get_marks(article_id)
    return MarksResponse(success=True, data=MarksData(**marks))


@router.put(
    "/rss/article/{article_id}/favorite",
    response_model=MarkActionResponse,
    summary="添加收藏 (幂等)",
)
async def add_favorite(
    article_id: int = Path(..., ge=1, description="文章 ID (articles.id,>=1)"),
):
    """
    将文章置为"已收藏"。重复调用保持 200 且状态不变 (幂等)。

    - 200: `{success: true}`
    - 404: 文章不存在 (不写入)
    """
    if not rss_store.article_exists(article_id):
        raise HTTPException(status_code=404, detail=_ARTICLE_NOT_FOUND)

    rss_store.set_favorite(article_id, True)
    return MarkActionResponse(success=True)


@router.delete(
    "/rss/article/{article_id}/favorite",
    response_model=MarkActionResponse,
    summary="取消收藏 (幂等)",
)
async def remove_favorite(
    article_id: int = Path(..., ge=1, description="文章 ID (articles.id,>=1)"),
):
    """
    将文章置为"未收藏"。重复调用或对未收藏文章调用保持 200 (幂等)。

    - 200: `{success: true}`
    - 404: 文章不存在 (不写入)
    """
    if not rss_store.article_exists(article_id):
        raise HTTPException(status_code=404, detail=_ARTICLE_NOT_FOUND)

    rss_store.set_favorite(article_id, False)
    return MarkActionResponse(success=True)


@router.put(
    "/rss/article/{article_id}/watchlist",
    response_model=MarkActionResponse,
    summary="加入待看 (幂等)",
)
async def add_watchlist(
    article_id: int = Path(..., ge=1, description="文章 ID (articles.id,>=1)"),
):
    """
    将文章置为"已加入待看"。重复调用保持 200 且状态不变 (幂等)。

    - 200: `{success: true}`
    - 404: 文章不存在 (不写入)
    """
    if not rss_store.article_exists(article_id):
        raise HTTPException(status_code=404, detail=_ARTICLE_NOT_FOUND)

    rss_store.set_watchlist(article_id, True)
    return MarkActionResponse(success=True)


@router.delete(
    "/rss/article/{article_id}/watchlist",
    response_model=MarkActionResponse,
    summary="移出待看 (幂等)",
)
async def remove_watchlist(
    article_id: int = Path(..., ge=1, description="文章 ID (articles.id,>=1)"),
):
    """
    将文章置为"未加入待看"。重复调用或对未加入待看的文章调用保持 200 (幂等)。

    - 200: `{success: true}`
    - 404: 文章不存在 (不写入)
    """
    if not rss_store.article_exists(article_id):
        raise HTTPException(status_code=404, detail=_ARTICLE_NOT_FOUND)

    rss_store.set_watchlist(article_id, False)
    return MarkActionResponse(success=True)


# ── 分页列表端点 ─────────────────────────────────────────


def _get_base_url(request: Request) -> str:
    """获取服务基础 URL，优先使用环境变量 SITE_URL，支持反向代理。"""
    site_url = os.getenv("SITE_URL", "").strip()
    if site_url:
        return site_url.rstrip("/")
    proto = request.headers.get("X-Forwarded-Proto", "http")
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", "localhost:5000")
    return f"{proto}://{host}"


def _format_mark_list_items(items: list, base_url: str) -> list:
    """将 store 层返回的 item 列表格式化为与 /api/rss/articles 一致的字段形状。"""
    result = []
    for a in items:
        head_img = proxy_image_url(a.get("head_img", ""), base_url)
        cover = proxy_image_url(a.get("cover", ""), base_url)
        nickname = a.get("nickname", "")
        # 与 /api/rss/articles 保持一致：无订阅信息时回退为"单篇下载"
        if not nickname and not a.get("fakeid"):
            nickname = "单篇下载"
        elif not nickname:
            nickname = "单篇下载"
        result.append({
            "id": a.get("id", 0),
            "title": a.get("title", ""),
            "link": a.get("link", ""),
            "digest": a.get("digest", ""),
            "cover": cover,
            "author": a.get("author", ""),
            "publish_time": a.get("publish_time", 0),
            "nickname": nickname,
            "head_img": head_img,
            "fakeid": a.get("fakeid", ""),
            "read_at": a.get("read_at", 0),
            "is_favorite": bool(a.get("is_favorite", False)),
            "is_watchlist": bool(a.get("is_watchlist", False)),
        })
    return result


@router.get(
    "/rss/marks/favorites",
    response_model=MarksListResponse,
    summary="分页列出已收藏文章",
)
async def list_favorites(
    request: Request,
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量 (1-100)"),
    unread_only: bool = Query(False, description="仅返回未读"),
):
    """
    分页列出所有已收藏文章，按最近一次标记时间倒序排列。

    响应结构与 `/api/rss/articles` 完全一致，前端可复用同一渲染函数。

    - 200: `{success, data, total, page, page_size, total_pages}`
    - 400: 参数不合法（由全局 validation handler 映射）
    """
    result = rss_store.list_favorites_paged(page, page_size, unread_only=unread_only)
    items = result["items"]
    total = result["total"]
    total_pages = math.ceil(total / page_size) if total > 0 else 0

    base_url = _get_base_url(request)
    data = _format_mark_list_items(items, base_url)

    return MarksListResponse(
        success=True,
        data=data,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get(
    "/rss/marks/watchlist",
    response_model=MarksListResponse,
    summary="分页列出已加入待看文章",
)
async def list_watchlist(
    request: Request,
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量 (1-100)"),
    unread_only: bool = Query(False, description="仅返回未读"),
):
    """
    分页列出所有已加入待看的文章，按最近一次标记时间倒序排列。

    响应结构与 `/api/rss/articles` 完全一致，前端可复用同一渲染函数。

    - 200: `{success, data, total, page, page_size, total_pages}`
    - 400: 参数不合法（由全局 validation handler 映射）
    """
    result = rss_store.list_watchlist_paged(page, page_size, unread_only=unread_only)
    items = result["items"]
    total = result["total"]
    total_pages = math.ceil(total / page_size) if total > 0 else 0

    base_url = _get_base_url(request)
    data = _format_mark_list_items(items, base_url)

    return MarksListResponse(
        success=True,
        data=data,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )
