#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
管理路由 - FastAPI版本
"""

import logging
import time
import json
import asyncio
import random

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from typing import Optional
import httpx

from utils.auth_manager import auth_manager
from utils.webhook import webhook
from utils import rss_store

logger = logging.getLogger(__name__)

router = APIRouter()

class StatusResponse(BaseModel):
    """状态响应模型"""
    authenticated: bool
    loggedIn: bool
    account: str
    nickname: Optional[str] = ""
    fakeid: Optional[str] = ""
    expireTime: Optional[int] = 0
    isExpired: Optional[bool] = False
    status: str


async def _verify_token_with_wechat(token: str, cookie: str) -> bool:
    """
    向微信服务端发一次轻量请求，验证 token 是否真正有效。
    使用 searchbiz 接口做探测（空查询，不会产生实际业务影响）。
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://mp.weixin.qq.com/cgi-bin/searchbiz",
                params={
                    "action": "search_biz",
                    "token": token,
                    "lang": "zh_CN",
                    "f": "json",
                    "ajax": 1,
                    "query": "test",
                    "begin": 0,
                    "count": 1,
                },
                headers={
                    "Cookie": cookie,
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                },
            )
            result = resp.json()
            ret = result.get("base_resp", {}).get("ret", -1)
            # ret=0 表示 token 有效
            return ret == 0
    except Exception as e:
        logger.warning("验证 token 时请求异常: %s", e)
        # 网络异常时不误判为过期，返回 None 表示无法确认
        return None


@router.get("/status", response_model=StatusResponse, summary="获取登录状态")
async def get_status(verify: bool = Query(False, description="是否向微信服务端验证 token 真实有效性")):
    """
    获取当前登录状态。
    
    加 ?verify=true 会实际调用微信 API 验证 token 是否有效（稍慢但准确）。
    不加则只检查本地凭证和过期时间（快但可能不准）。
    """
    status = auth_manager.get_status()
    
    # 如果本地凭证存在且未过期，且请求了真实验证
    if verify and status.get("authenticated") and not status.get("isExpired"):
        creds = auth_manager.get_credentials()
        if creds:
            is_valid = await _verify_token_with_wechat(
                creds.get("token", ""), creds.get("cookie", "")
            )
            if is_valid is False:
                # 微信服务端确认 token 已失效
                status["isExpired"] = True
                status["status"] = "登录已失效（微信服务端验证不通过），请重新登录"
                logger.warning("Token 验证失败，登录已实际失效: %s", status.get("nickname", ""))
                
                # 发送飞书通知
                await webhook.notify('login_expired', {
                    'nickname': creds.get("nickname", "未知账号"),
                    'message': '管理页面验证发现登录已失效，请重新登录',
                })
    
    return status

@router.post("/logout", summary="退出登录")
async def logout():
    """
    退出登录，清除凭证
    
    Returns:
        操作结果
    """
    success = auth_manager.clear_credentials()
    if success:
        return {"success": True, "message": "已退出登录"}
    else:
        return {"success": False, "message": "退出登录失败"}


# ── 分类管理 ─────────────────────────────────────────────

class CategoryItem(BaseModel):
    id: int
    name: str
    description: str
    color: str
    sort_order: int
    subscription_count: int
    created_at: int


class CreateCategoryRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=50, description="分类名称")
    description: str = Field("", max_length=200, description="分类描述")
    color: str = Field("blue", description="颜色标识")


class UpdateCategoryRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    description: Optional[str] = None
    color: Optional[str] = None


class SetCategoryRequest(BaseModel):
    category_id: Optional[int] = Field(None, description="分类ID，null表示取消分类")


@router.get("/categories", summary="获取分类列表")
async def get_categories():
    """获取所有分类"""
    categories = rss_store.list_categories()
    return {
        "categories": [
            CategoryItem(
                id=c["id"],
                name=c["name"],
                description=c["description"],
                color=c["color"],
                sort_order=c["sort_order"],
                subscription_count=c["subscription_count"],
                created_at=c["created_at"],
            )
            for c in categories
        ]
    }


@router.post("/categories", summary="创建分类")
async def create_category(req: CreateCategoryRequest):
    """创建新分类"""
    from fastapi import HTTPException
    category_id = rss_store.create_category(
        name=req.name,
        description=req.description,
        color=req.color,
    )
    if category_id:
        return {"success": True, "id": category_id, "message": f"分类 '{req.name}' 创建成功"}
    raise HTTPException(status_code=400, detail="分类名称已存在")


@router.patch("/categories/{category_id}", summary="更新分类")
async def update_category(category_id: int, req: UpdateCategoryRequest):
    """更新分类信息"""
    from fastapi import HTTPException
    success = rss_store.update_category(
        category_id=category_id,
        name=req.name,
        description=req.description,
        color=req.color,
    )
    if success:
        return {"success": True, "message": "分类已更新"}
    raise HTTPException(status_code=404, detail="分类不存在")


@router.delete("/categories/{category_id}", summary="删除分类")
async def delete_category(category_id: int):
    """删除分类（订阅会自动解除关联）"""
    from fastapi import HTTPException
    success = rss_store.delete_category(category_id)
    if success:
        return {"success": True, "message": "分类已删除"}
    raise HTTPException(status_code=404, detail="分类不存在")


@router.put("/subscriptions/{fakeid}/category", summary="设置订阅分类")
async def set_subscription_category(fakeid: str, req: SetCategoryRequest):
    """设置订阅的分类"""
    from fastapi import HTTPException
    if req.category_id is not None:
        category = rss_store.get_category(req.category_id)
        if not category:
            raise HTTPException(status_code=404, detail="分类不存在")

    success = rss_store.set_subscription_category(fakeid, req.category_id)
    if success:
        return {"success": True, "message": "分类已设置"}
    raise HTTPException(status_code=404, detail="订阅不存在")


# ── 黑名单管理 ─────────────────────────────────────────────

class BlacklistItem(BaseModel):
    id: int
    fakeid: str
    nickname: str
    reason: str
    verification_count: int
    is_active: bool
    blacklisted_at: int
    unblacklisted_at: Optional[int]
    note: str


class AddBlacklistRequest(BaseModel):
    fakeid: str = Field(..., description="公众号ID")
    nickname: str = Field("", description="公众号名称")
    reason: str = Field("manual", description="加入原因")
    note: str = Field("", description="备注")


@router.get("/blacklist", summary="获取黑名单列表")
async def get_blacklist():
    """获取公众号黑名单列表"""
    blacklist = rss_store.get_blacklist()
    return {
        "blacklist": [
            BlacklistItem(
                id=bl["id"],
                fakeid=bl["fakeid"],
                nickname=bl["nickname"],
                reason=bl["reason"],
                verification_count=bl["verification_count"],
                is_active=bool(bl["is_active"]),
                blacklisted_at=bl["blacklisted_at"],
                unblacklisted_at=bl["unblacklisted_at"],
                note=bl["note"],
            )
            for bl in blacklist
        ]
    }


@router.post("/blacklist", summary="添加到黑名单")
async def add_to_blacklist(req: AddBlacklistRequest):
    """手动添加公众号到黑名单"""
    success = rss_store.add_to_blacklist(
        fakeid=req.fakeid,
        nickname=req.nickname,
        reason=req.reason,
        note=req.note or "手动添加",
    )
    if success:
        return {"success": True, "message": f"已将 {req.nickname or req.fakeid} 加入黑名单"}
    return {"success": False, "message": "添加失败"}


@router.delete("/blacklist/{fakeid}", summary="从黑名单移除")
async def remove_from_blacklist(fakeid: str):
    """从黑名单移除公众号（标记为非活跃，恢复轮询）"""
    success = rss_store.remove_from_blacklist(fakeid)
    if success:
        return {"success": True, "message": "已从黑名单移除"}
    return {"success": False, "message": "移除失败，记录不存在"}


@router.delete("/blacklist/record/{blacklist_id}", summary="永久删除黑名单记录")
async def delete_blacklist_record(blacklist_id: int):
    """永久删除黑名单记录（仅可删除非活跃记录）"""
    success = rss_store.delete_blacklist_record(blacklist_id)
    if success:
        return {"success": True, "message": "记录已删除"}
    return {"success": False, "message": "删除失败，记录不存在或仍在生效中"}


# ── 历史文章获取 ─────────────────────────────────────────────


class FetchHistoryRequest(BaseModel):
    fakeid: str = Field(..., description="公众号ID")
    count: int = Field(1, ge=1, le=100, description="获取数量，1-100篇")


class FetchHistoryResponse(BaseModel):
    success: bool
    message: str
    fetched_count: int = 0
    new_count: int = 0


@router.post("/history/fetch", response_model=FetchHistoryResponse,
             summary="获取历史文章")
async def fetch_history_articles(req: FetchHistoryRequest):
    """
    获取公众号的历史文章并存入数据库。

    历史文章：订阅时间之前发布的文章（publish_time < subscription.created_at）。
    保存时标记为 source='deep_fetch'，与轮询器拉取的常规文章分离，
    通过 `/api/rss/{fakeid}/history` 独立 RSS 订阅。
    """
    status = auth_manager.get_status()
    if not status.get("authenticated"):
        return FetchHistoryResponse(
            success=False,
            message="未登录，请先扫码登录",
        )

    subscriptions = rss_store.list_subscriptions()
    sub = next((s for s in subscriptions if s["fakeid"] == req.fakeid), None)
    if not sub:
        return FetchHistoryResponse(
            success=False,
            message="订阅不存在，请先添加订阅",
        )

    try:
        fetched_count, new_count = await _fetch_history_internal(
            fakeid=req.fakeid,
            target_count=req.count,
        )
        return FetchHistoryResponse(
            success=True,
            message=f"获取完成，共获取 {fetched_count} 篇，新增 {new_count} 篇",
            fetched_count=fetched_count,
            new_count=new_count,
        )
    except Exception as e:
        logger.exception("获取历史文章失败")
        return FetchHistoryResponse(
            success=False,
            message=f"获取失败: {str(e)}",
        )


async def _fetch_history_internal(fakeid: str, target_count: int) -> tuple:
    """
    内部历史文章获取逻辑。

    历史文章定义：订阅时间之前发布的文章（publish_time < subscription.created_at）。

    流程：
    1. 获取订阅时间和数据库中已有的历史文章数量
    2. 从已有历史文章的位置开始翻页，避免重复拉取
    3. 只保存订阅前发布的文章
    4. 达到目标数量或无更多历史文章时停止

    返回 (fetched_count, new_count)。
    """
    creds = auth_manager.get_credentials()
    if not creds or not creds.get("token"):
        raise ValueError("登录凭证无效")

    sub = rss_store.get_subscription(fakeid)
    if not sub:
        raise ValueError("订阅不存在")
    sub_time = sub.get("created_at", 0) or int(time.time())

    existing_historical = rss_store.count_historical_articles(fakeid)

    historical_articles: list = []
    batch_size = 10
    start_batch = existing_historical // batch_size
    batch_num = start_batch
    max_batches = start_batch + 50  # 最多再翻 50 页

    while batch_num < max_batches and len(historical_articles) < target_count:
        begin = batch_num * batch_size

        params = {
            "begin": begin,
            "count": batch_size,
            "fakeid": fakeid,
            "type": "101_1",
            "free_publish_type": 1,
            "sub_action": "list_ex",
            "token": creds["token"],
            "lang": "zh_CN",
            "f": "json",
            "ajax": 1,
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://mp.weixin.qq.com/",
            "Cookie": creds["cookie"],
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                "https://mp.weixin.qq.com/cgi-bin/appmsgpublish",
                params=params,
                headers=headers,
            )
            resp.raise_for_status()
            result = resp.json()

        base_resp = result.get("base_resp", {})
        ret_code = base_resp.get("ret", -1)

        if ret_code == 200003:
            raise ValueError("触发验证码，请稍后重试")
        if ret_code != 0:
            raise ValueError(f"微信API错误: ret={ret_code}")

        publish_page = result.get("publish_page", {})
        if isinstance(publish_page, str):
            try:
                publish_page = json.loads(publish_page)
            except (json.JSONDecodeError, ValueError):
                batch_num += 1
                continue

        if not isinstance(publish_page, dict):
            batch_num += 1
            continue

        batch_articles = []

        for item in publish_page.get("publish_list", []):
            publish_info = item.get("publish_info", {})
            if isinstance(publish_info, str):
                try:
                    publish_info = json.loads(publish_info)
                except (json.JSONDecodeError, ValueError):
                    continue
            if not isinstance(publish_info, dict):
                continue
            for a in publish_info.get("appmsgex", []):
                publish_time = a.get("update_time", 0)
                # 只保存订阅时间之前发布的文章（历史文章）
                if publish_time < sub_time:
                    batch_articles.append({
                        "aid": a.get("aid", ""),
                        "title": a.get("title", ""),
                        "link": a.get("link", ""),
                        "digest": a.get("digest", ""),
                        "cover": a.get("cover", ""),
                        "author": a.get("author", ""),
                        "publish_time": publish_time,
                    })

        if batch_articles:
            historical_articles.extend(batch_articles)

        articles_in_page = len(publish_page.get("publish_list", []))
        if articles_in_page < batch_size:
            # 没有更多文章了
            break

        batch_num += 1

        if len(historical_articles) < target_count:
            await asyncio.sleep(random.uniform(2, 4))

    # 截取到目标数量
    historical_articles = historical_articles[:target_count]

    # 保存到数据库（去重），标记为历史文章 'deep_fetch'
    new_count = rss_store.save_articles(
        fakeid, historical_articles, source='deep_fetch'
    )

    return len(historical_articles), new_count
