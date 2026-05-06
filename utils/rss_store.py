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


def _migrate_remove_fk(conn: sqlite3.Connection):
    """迁移：检测并去除 articles 表的外键约束。

    SQLite 不支持 ALTER TABLE ... DROP CONSTRAINT，因此需要通过
    重建表的方式去除外键。迁移过程在事务中执行，确保数据不丢失。
    """
    fk_list = conn.execute("PRAGMA foreign_key_list(articles)").fetchall()
    if not fk_list:
        return  # 无外键，无需迁移

    logger.info("检测到 articles 表存在外键约束，开始迁移去除外键...")

    # 迁移期间关闭外键检查，避免重建过程中触发约束
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.execute("""
            CREATE TABLE articles_new (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                fakeid        TEXT NOT NULL,
                aid           TEXT NOT NULL DEFAULT '',
                title         TEXT NOT NULL DEFAULT '',
                link          TEXT NOT NULL DEFAULT '',
                digest        TEXT NOT NULL DEFAULT '',
                cover         TEXT NOT NULL DEFAULT '',
                author        TEXT NOT NULL DEFAULT '',
                content       TEXT NOT NULL DEFAULT '',
                plain_content TEXT NOT NULL DEFAULT '',
                publish_time  INTEGER NOT NULL DEFAULT 0,
                fetched_at    INTEGER NOT NULL,
                read_at       INTEGER NOT NULL DEFAULT 0,
                UNIQUE(fakeid, link)
            )
        """)
        conn.execute("""
            INSERT INTO articles_new
                (id, fakeid, aid, title, link, digest, cover, author,
                 content, plain_content, publish_time, fetched_at, read_at)
            SELECT id, fakeid, aid, title, link, digest, cover, author,
                   content, plain_content, publish_time, fetched_at, read_at
            FROM articles
        """)
        conn.execute("DROP TABLE articles")
        conn.execute("ALTER TABLE articles_new RENAME TO articles")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_articles_fakeid_time
                ON articles(fakeid, publish_time DESC)
        """)
        conn.commit()
        logger.info("articles 表外键约束迁移完成")
    except Exception:
        conn.rollback()
        logger.exception("articles 表外键迁移失败")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


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
            UNIQUE(fakeid, link)
        );

        CREATE INDEX IF NOT EXISTS idx_articles_fakeid_time
            ON articles(fakeid, publish_time DESC);
    """)
    conn.commit()

    # 对已有数据库执行迁移：去除 articles 表的外键约束
    _migrate_remove_fk(conn)

    # 对已有数据库执行迁移：添加 source 字段（区分轮询文章与历史文章）
    cursor = conn.execute("PRAGMA table_info(articles)")
    columns = [row[1] for row in cursor.fetchall()]
    if "source" not in columns:
        logger.info("Adding source column to articles table")
        conn.execute(
            "ALTER TABLE articles ADD COLUMN source TEXT NOT NULL DEFAULT 'poll'"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source)"
        )
        conn.commit()
        logger.info("Added source column and index to articles table")

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
        # 手动删除关联文章（去除外键后不再有 ON DELETE CASCADE）
        conn.execute("DELETE FROM articles WHERE fakeid=?", (fakeid,))
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

def save_articles(fakeid: str, articles: List[Dict], source: str = "poll") -> int:
    """
    批量保存文章，返回新增数量。
    If an article already exists but has empty content, update it with new content.

    Args:
        fakeid: 公众号ID
        articles: 文章列表
        source: 文章来源标记，'poll'为轮询器拉取，'deep_fetch'为历史文章获取
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
                    "content, plain_content, publish_time, fetched_at, source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
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
                        source,
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


def get_regular_articles(fakeid: str, limit: int = 50) -> List[Dict]:
    """
    获取常规文章（轮询器拉取的文章）
    只返回 source='poll' 的文章，不包含历史文章
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM articles WHERE fakeid=? AND source='poll' "
            "ORDER BY publish_time DESC LIMIT ?",
            (fakeid, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_historical_articles(fakeid: str, limit: int = 500, offset: int = 0) -> List[Dict]:
    """
    获取历史文章（通过"获取历史文章"功能拉取的文章）
    返回 source='deep_fetch' 的文章，用于独立的历史 RSS，支持分页
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM articles WHERE fakeid=? AND source='deep_fetch' "
            "ORDER BY publish_time DESC LIMIT ? OFFSET ?",
            (fakeid, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_historical_articles(fakeid: str) -> int:
    """统计历史文章数量（source='deep_fetch'的文章）"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM articles WHERE fakeid=? AND source='deep_fetch'",
            (fakeid,),
        ).fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def get_articles_paged(fakeid: str, page: int = 1, page_size: int = 10,
                       unread_only: bool = False) -> dict:
    """分页获取指定公众号的文章"""
    conn = _get_conn()
    try:
        where = "WHERE fakeid=?"
        params = [fakeid]
        if unread_only:
            where += " AND read_at = 0"
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM articles " + where, params
        ).fetchone()["cnt"]
        offset = (page - 1) * page_size
        rows = conn.execute(
            "SELECT * FROM articles " + where +
            " ORDER BY publish_time DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()
        return {"items": [dict(r) for r in rows], "total": total}
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
    """Get latest articles across all subscriptions, sorted by publish_time desc.

    只返回轮询器拉取的文章（source='poll'），不包含历史文章，
    避免历史文章大量涌入聚合 RSS。
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM articles WHERE source='poll' "
            "ORDER BY publish_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_articles_paged(page: int = 1, page_size: int = 10,
                           unread_only: bool = False,
                           standalone_only: bool = False) -> dict:
    """分页获取所有文章。

    standalone_only=True 时只返回 fakeid 不在 subscriptions 表中的文章（单篇下载文章）。
    """
    conn = _get_conn()
    try:
        conditions = []
        if unread_only:
            conditions.append("read_at = 0")
        if standalone_only:
            conditions.append(
                "fakeid NOT IN (SELECT fakeid FROM subscriptions)"
            )
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM articles " + where
        ).fetchone()["cnt"]
        offset = (page - 1) * page_size
        rows = conn.execute(
            "SELECT * FROM articles " + where +
            " ORDER BY publish_time DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
        return {"items": [dict(r) for r in rows], "total": total}
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


def get_article_by_link(fakeid: str, link: str) -> Optional[Dict]:
    """根据 fakeid 和 link 查询文章记录，用于下载前的去重检查。"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM articles WHERE fakeid=? AND link=?",
            (fakeid, link),
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


def mark_read_by_fakeid(fakeid: str) -> int:
    """标记指定公众号的所有文章为已读，返回标记数量"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE articles SET read_at = ? WHERE fakeid = ? AND read_at = 0",
            (int(time.time()), fakeid),
        )
        conn.commit()
        return conn.total_changes
    finally:
        conn.close()


