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
from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional
import httpx
from utils.auth_manager import auth_manager
from utils.webhook import webhook

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
