#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
管理路由 - FastAPI版本
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from utils.auth_manager import auth_manager
from utils import rss_store

router = APIRouter()


# ── 状态管理 ─────────────────────────────────────────────

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


@router.get("/status", response_model=StatusResponse, summary="获取登录状态")
async def get_status():
    """获取当前登录状态"""
    return auth_manager.get_status()


@router.post("/logout", summary="退出登录")
async def logout():
    """退出登录，清除凭证"""
    success = auth_manager.clear_credentials()
    if success:
        return {"success": True, "message": "已退出登录"}
    else:
        return {"success": False, "message": "退出登录失败"}


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
        note=req.note or "手动添加"
    )
    if success:
        return {"success": True, "message": f"已将 {req.nickname or req.fakeid} 加入黑名单"}
    return {"success": False, "message": "添加失败"}


@router.delete("/blacklist/{fakeid}", summary="从黑名单移除")
async def remove_from_blacklist(fakeid: str):
    """从黑名单移除公众号（标记为非活跃）"""
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
    color: str = Field("blue", description="颜色: blue, green, red, purple, orange, gray")


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
    category_id = rss_store.create_category(
        name=req.name,
        description=req.description,
        color=req.color
    )
    if category_id:
        return {"success": True, "id": category_id, "message": f"分类 '{req.name}' 创建成功"}
    raise HTTPException(status_code=400, detail="分类名称已存在")


@router.patch("/categories/{category_id}", summary="更新分类")
async def update_category(category_id: int, req: UpdateCategoryRequest):
    """更新分类信息"""
    success = rss_store.update_category(
        category_id=category_id,
        name=req.name,
        description=req.description,
        color=req.color
    )
    if success:
        return {"success": True, "message": "分类已更新"}
    raise HTTPException(status_code=404, detail="分类不存在")


@router.delete("/categories/{category_id}", summary="删除分类")
async def delete_category(category_id: int):
    """删除分类（订阅会自动解除关联）"""
    success = rss_store.delete_category(category_id)
    if success:
        return {"success": True, "message": "分类已删除"}
    raise HTTPException(status_code=404, detail="分类不存在")


@router.get("/categories/{category_id}/subscriptions", summary="获取分类下的订阅")
async def get_category_subscriptions(category_id: int):
    """获取分类下的所有订阅"""
    category = rss_store.get_category(category_id)
    if not category:
        raise HTTPException(status_code=404, detail="分类不存在")
    
    subscriptions = rss_store.get_subscriptions_by_category(category_id)
    return {
        "category": CategoryItem(
            id=category["id"],
            name=category["name"],
            description=category["description"],
            color=category["color"],
            sort_order=category["sort_order"],
            subscription_count=len(subscriptions),
            created_at=category["created_at"],
        ),
        "subscriptions": subscriptions
    }


@router.put("/subscriptions/{fakeid}/category", summary="设置订阅分类")
async def set_subscription_category(fakeid: str, req: SetCategoryRequest):
    """设置订阅的分类"""
    # 如果指定了分类，验证分类存在
    if req.category_id is not None:
        category = rss_store.get_category(req.category_id)
        if not category:
            raise HTTPException(status_code=404, detail="分类不存在")
    
    success = rss_store.set_subscription_category(fakeid, req.category_id)
    if success:
        return {"success": True, "message": "分类已设置"}
    raise HTTPException(status_code=404, detail="订阅不存在")
