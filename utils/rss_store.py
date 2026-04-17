#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
RSS 数据存储 — SQLite
管理订阅列表和文章缓存
"""

import sqlite3
import time
import logging
import os
from pathlib import Path
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# Database path: configurable via env var, defaults to ./data/rss.db
_default_db = Path(__file__).parent.parent / "data" / "rss.db"
DB_PATH = Path(os.getenv("RSS_DB_PATH", str(_default_db)))


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """建表（幂等）"""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            fakeid      TEXT PRIMARY KEY,
            nickname    TEXT NOT NULL DEFAULT '',
            alias       TEXT NOT NULL DEFAULT '',
            head_img    TEXT NOT NULL DEFAULT '',
            created_at  INTEGER NOT NULL,
            last_poll   INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS articles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fakeid      TEXT NOT NULL,
            aid         TEXT NOT NULL DEFAULT '',
            title       TEXT NOT NULL DEFAULT '',
            link        TEXT NOT NULL DEFAULT '',
            digest      TEXT NOT NULL DEFAULT '',
            cover       TEXT NOT NULL DEFAULT '',
            author      TEXT NOT NULL DEFAULT '',
            content     TEXT NOT NULL DEFAULT '',
            plain_content TEXT NOT NULL DEFAULT '',
            publish_time INTEGER NOT NULL DEFAULT 0,
            fetched_at  INTEGER NOT NULL,
            read_at     INTEGER NOT NULL DEFAULT 0,
            UNIQUE(fakeid, link),
            FOREIGN KEY (fakeid) REFERENCES subscriptions(fakeid) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_articles_fakeid_time
            ON articles(fakeid, publish_time DESC);
    """)
    conn.commit()
    
    # 迁移：为已有数据库补上 read_at 列
    try:
        conn.execute("ALTER TABLE articles ADD COLUMN read_at INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        logger.info("Migration: added read_at column to articles table")
    except sqlite3.OperationalError:
        pass  # 列已存在，忽略
    
    conn.close()
    logger.info("RSS database initialized: %s", DB_PATH)


# ── 订阅管理 ─────────────────────────────────────────────

def add_subscription(fakeid: str, nickname: str = "",
                     alias: str = "", head_img: str = "") -> bool:
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions "
            "(fakeid, nickname, alias, head_img, created_at) VALUES (?,?,?,?,?)",
            (fakeid, nickname, alias, head_img, int(time.time())),
        )
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def remove_subscription(fakeid: str) -> bool:
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM subscriptions WHERE fakeid=?", (fakeid,))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def list_subscriptions() -> List[Dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM articles a WHERE a.fakeid=s.fakeid) AS article_count, "
            "(SELECT a2.title FROM articles a2 WHERE a2.fakeid=s.fakeid "
            " ORDER BY a2.publish_time DESC LIMIT 1) AS latest_title, "
            "(SELECT a2.publish_time FROM articles a2 WHERE a2.fakeid=s.fakeid "
            " ORDER BY a2.publish_time DESC LIMIT 1) AS latest_publish_time, "
            "(SELECT a2.id FROM articles a2 WHERE a2.fakeid=s.fakeid "
            " ORDER BY a2.publish_time DESC LIMIT 1) AS latest_article_id "
            "FROM subscriptions s ORDER BY s.created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_subscription(fakeid: str) -> Optional[Dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE fakeid=?", (fakeid,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_last_poll(fakeid: str):
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE subscriptions SET last_poll=? WHERE fakeid=?",
            (int(time.time()), fakeid),
        )
        conn.commit()
    finally:
        conn.close()


# ── 文章缓存 ─────────────────────────────────────────────

def save_articles(fakeid: str, articles: List[Dict]) -> int:
    """
    批量保存文章，返回新增数量。
    If an article already exists but has empty content, update it with new content.
    """
    conn = _get_conn()
    inserted = 0
    try:
        for a in articles:
            content = a.get("content", "")
            plain_content = a.get("plain_content", "")
            try:
                cursor = conn.execute(
                    "INSERT INTO articles "
                    "(fakeid, aid, title, link, digest, cover, author, "
                    "content, plain_content, publish_time, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(fakeid, link) DO UPDATE SET "
                    "content = CASE WHEN excluded.content != '' AND articles.content = '' "
                    "  THEN excluded.content ELSE articles.content END, "
                    "plain_content = CASE WHEN excluded.plain_content != '' AND articles.plain_content = '' "
                    "  THEN excluded.plain_content ELSE articles.plain_content END, "
                    "author = CASE WHEN excluded.author != '' AND articles.author = '' "
                    "  THEN excluded.author ELSE articles.author END",
                    (
                        fakeid,
                        a.get("aid", ""),
                        a.get("title", ""),
                        a.get("link", ""),
                        a.get("digest", ""),
                        a.get("cover", ""),
                        a.get("author", ""),
                        content,
                        plain_content,
                        a.get("publish_time", 0),
                        int(time.time()),
                    ),
                )
                if cursor.rowcount > 0:
                    inserted += 1
            except sqlite3.IntegrityError:
                pass
        conn.commit()
        return inserted
    finally:
        conn.close()


def get_articles(fakeid: str, limit: int = 20) -> List[Dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM articles WHERE fakeid=? "
            "ORDER BY publish_time DESC LIMIT ?",
            (fakeid, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_fakeids() -> List[str]:
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT fakeid FROM subscriptions").fetchall()
        return [r["fakeid"] for r in rows]
    finally:
        conn.close()


def get_all_articles(limit: int = 50) -> List[Dict]:
    """Get latest articles across all subscriptions, sorted by publish_time desc."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM articles ORDER BY publish_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_article_by_id(article_id: int) -> Optional[Dict]:
    """Get a single article by its ID."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM articles WHERE id=?", (article_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_unread_count() -> int:
    """获取未读文章总数"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM articles WHERE read_at = 0"
        ).fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def get_unread_counts_by_fakeid() -> Dict[str, int]:
    """获取每个公众号的未读文章数"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT fakeid, COUNT(*) AS cnt FROM articles WHERE read_at = 0 GROUP BY fakeid"
        ).fetchall()
        return {r["fakeid"]: r["cnt"] for r in rows}
    finally:
        conn.close()


def mark_article_read(article_id: int) -> bool:
    """标记单篇文章为已读"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE articles SET read_at = ? WHERE id = ? AND read_at = 0",
            (int(time.time()), article_id),
        )
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def mark_all_read() -> int:
    """标记所有文章为已读，返回标记数量"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE articles SET read_at = ? WHERE read_at = 0",
            (int(time.time()),),
        )
        conn.commit()
        return conn.total_changes
    finally:
        conn.close()


