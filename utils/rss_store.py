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

    # 迁移：subscriptions 表添加 sort_order 字段（用于阅读器侧栏自定义排序）
    cursor = conn.execute("PRAGMA table_info(subscriptions)")
    columns = [row[1] for row in cursor.fetchall()]
    if "sort_order" not in columns:
        logger.info("Adding sort_order column to subscriptions table")
        conn.execute(
            "ALTER TABLE subscriptions ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_sort_order "
            "ON subscriptions(sort_order)"
        )
        conn.commit()
        logger.info("Added sort_order column to subscriptions table")

    # 文章标记（收藏 / 待看）— 两张独立表，主键即 article_id
    # 与现有 subscriptions / articles Schema 解耦，幂等建表/建索引/建触发器
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS article_favorites (
            article_id INTEGER PRIMARY KEY,
            created_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_article_favorites_created
            ON article_favorites(created_at DESC);

        CREATE TABLE IF NOT EXISTS article_watchlist (
            article_id INTEGER PRIMARY KEY,
            created_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_article_watchlist_created
            ON article_watchlist(created_at DESC);

        -- 级联清理：文章删除时同步清理标记，避免悬空数据
        CREATE TRIGGER IF NOT EXISTS trg_cleanup_favorites_on_article_delete
        AFTER DELETE ON articles
        BEGIN
            DELETE FROM article_favorites WHERE article_id = OLD.id;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_cleanup_watchlist_on_article_delete
        AFTER DELETE ON articles
        BEGIN
            DELETE FROM article_watchlist WHERE article_id = OLD.id;
        END;
    """)
    conn.commit()

    conn.close()
    logger.info("RSS database initialized: %s", DB_PATH)


# ── 订阅管理 ─────────────────────────────────────────────

def add_subscription(fakeid: str, nickname: str = "",
                     alias: str = "", head_img: str = "") -> bool:
    conn = _get_conn()
    try:
        # 新订阅排在最前：sort_order = 当前最小值 - 10（没数据时用 -10）
        row = conn.execute(
            "SELECT MIN(sort_order) AS min_order FROM subscriptions"
        ).fetchone()
        min_order = row["min_order"] if row and row["min_order"] is not None else 0
        new_sort_order = min_order - 10
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions "
            "(fakeid, nickname, alias, head_img, created_at, sort_order) "
            "VALUES (?,?,?,?,?,?)",
            (fakeid, nickname, alias, head_img, int(time.time()), new_sort_order),
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
            "FROM subscriptions s ORDER BY s.sort_order ASC, s.created_at DESC"
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


def get_articles_paged(fakeid: str, page: int = 1, page_size: int = 10,
                       unread_only: bool = False) -> dict:
    """分页获取指定公众号的文章"""
    conn = _get_conn()
    try:
        where = "WHERE articles.fakeid=?"
        params = [fakeid]
        if unread_only:
            where += " AND articles.read_at = 0"
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM articles " + where, params
        ).fetchone()["cnt"]
        offset = (page - 1) * page_size
        rows = conn.execute(
            "SELECT articles.*, "
            "(af.article_id IS NOT NULL) AS is_favorite, "
            "(aw.article_id IS NOT NULL) AS is_watchlist "
            "FROM articles "
            "LEFT JOIN article_favorites af ON af.article_id = articles.id "
            "LEFT JOIN article_watchlist aw ON aw.article_id = articles.id "
            + where +
            " ORDER BY articles.publish_time DESC LIMIT ? OFFSET ?",
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
            conditions.append("articles.read_at = 0")
        if standalone_only:
            conditions.append(
                "articles.fakeid NOT IN (SELECT fakeid FROM subscriptions)"
            )
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM articles " + where
        ).fetchone()["cnt"]
        offset = (page - 1) * page_size
        rows = conn.execute(
            "SELECT articles.*, "
            "(af.article_id IS NOT NULL) AS is_favorite, "
            "(aw.article_id IS NOT NULL) AS is_watchlist "
            "FROM articles "
            "LEFT JOIN article_favorites af ON af.article_id = articles.id "
            "LEFT JOIN article_watchlist aw ON aw.article_id = articles.id "
            + where +
            " ORDER BY articles.publish_time DESC LIMIT ? OFFSET ?",
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




def reorder_subscriptions(fakeids: List[str]) -> int:
    """
    根据给定的 fakeid 列表顺序更新 sort_order。
    列表中第 0 位得到 sort_order=10，第 1 位 20，以此类推。
    不在列表中的订阅保持原值（排到后面）。

    返回更新的记录数。
    """
    if not fakeids:
        return 0
    conn = _get_conn()
    try:
        conn.execute("BEGIN")
        updated = 0
        for idx, fakeid in enumerate(fakeids):
            cursor = conn.execute(
                "UPDATE subscriptions SET sort_order=? WHERE fakeid=?",
                ((idx + 1) * 10, fakeid),
            )
            if cursor.rowcount > 0:
                updated += 1
        conn.commit()
        return updated
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── 文章标记（收藏 / 待看） ─────────────────────────────

def article_exists(article_id: int) -> bool:
    """检查文章是否存在于 articles 表中（用于 Mark_API 的 404 前置校验）。"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM articles WHERE id=? LIMIT 1",
            (article_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_marks(article_id: int) -> Dict[str, bool]:
    """查询指定文章的收藏 / 待看标记状态。

    在同一连接内执行两条独立 SELECT，返回
    ``{"favorite": bool, "watchlist": bool}``。
    """
    conn = _get_conn()
    try:
        fav_row = conn.execute(
            "SELECT 1 FROM article_favorites WHERE article_id=? LIMIT 1",
            (article_id,),
        ).fetchone()
        watch_row = conn.execute(
            "SELECT 1 FROM article_watchlist WHERE article_id=? LIMIT 1",
            (article_id,),
        ).fetchone()
        return {
            "favorite": fav_row is not None,
            "watchlist": watch_row is not None,
        }
    finally:
        conn.close()


def set_favorite(article_id: int, value: bool) -> None:
    """设置 / 取消某篇文章的收藏标记。

    - ``value=True``  → ``INSERT OR IGNORE``，幂等地写入一条记录；
      已存在时不更新 ``created_at``。
    - ``value=False`` → ``DELETE``，幂等地清除；记录不存在时静默返回。

    写操作为单条 SQL，SQLite 天然原子。
    """
    conn = _get_conn()
    try:
        if value:
            conn.execute(
                "INSERT OR IGNORE INTO article_favorites"
                " (article_id, created_at) VALUES (?, ?)",
                (article_id, int(time.time())),
            )
        else:
            conn.execute(
                "DELETE FROM article_favorites WHERE article_id=?",
                (article_id,),
            )
        conn.commit()
    finally:
        conn.close()


def set_watchlist(article_id: int, value: bool) -> None:
    """设置 / 取消某篇文章的待看标记。

    语义与 :func:`set_favorite` 对称，作用于 ``article_watchlist`` 表。
    """
    conn = _get_conn()
    try:
        if value:
            conn.execute(
                "INSERT OR IGNORE INTO article_watchlist"
                " (article_id, created_at) VALUES (?, ?)",
                (article_id, int(time.time())),
            )
        else:
            conn.execute(
                "DELETE FROM article_watchlist WHERE article_id=?",
                (article_id,),
            )
        conn.commit()
    finally:
        conn.close()


def list_favorites_paged(page: int, page_size: int,
                         unread_only: bool = False) -> Dict:
    """分页列出已收藏文章，按最近一次标记时间倒序（``af.created_at DESC``）。

    SQL 以 ``article_favorites`` 为主，``JOIN articles`` 获取文章字段，
    ``LEFT JOIN subscriptions`` 补齐 ``nickname`` / ``head_img``，
    ``LEFT JOIN article_watchlist`` 取 ``is_watchlist`` 的实际值。

    ``unread_only=True`` 时仅返回 ``a.read_at = 0`` 的记录，COUNT 同步过滤。

    返回 ``{"items": [...], "total": N}``。每个 item 的字段集合与
    ``get_all_articles_paged`` 的 item 一致（``articles`` 表全部列，
    含 ``read_at`` 等），并额外携带 ``nickname`` / ``head_img`` /
    ``is_favorite=True`` / ``is_watchlist=<实际值>``。
    """
    conn = _get_conn()
    try:
        where = " WHERE a.read_at = 0" if unread_only else ""
        total_row = conn.execute(
            "SELECT COUNT(*) AS cnt "
            "FROM article_favorites af "
            "JOIN articles a ON a.id = af.article_id" + where
        ).fetchone()
        total = total_row["cnt"] if total_row else 0
        offset = (page - 1) * page_size
        rows = conn.execute(
            "SELECT a.*, "
            "       s.nickname AS nickname, "
            "       s.head_img AS head_img, "
            "       1 AS is_favorite, "
            "       (aw.article_id IS NOT NULL) AS is_watchlist "
            "FROM article_favorites af "
            "JOIN articles a ON a.id = af.article_id "
            "LEFT JOIN subscriptions s ON s.fakeid = a.fakeid "
            "LEFT JOIN article_watchlist aw ON aw.article_id = af.article_id "
            + where +
            " ORDER BY af.created_at DESC "
            "LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            # NULL 归一化：subscription 已被删除时保持空串，与现有 API 层行为一致
            if d.get("nickname") is None:
                d["nickname"] = ""
            if d.get("head_img") is None:
                d["head_img"] = ""
            # 布尔化：SQLite 返回 0/1
            d["is_favorite"] = True
            d["is_watchlist"] = bool(d.get("is_watchlist", 0))
            items.append(d)
        return {"items": items, "total": total}
    finally:
        conn.close()


def list_watchlist_paged(page: int, page_size: int,
                         unread_only: bool = False) -> Dict:
    """分页列出已加入待看的文章，按最近一次标记时间倒序（``aw.created_at DESC``）。

    语义与 :func:`list_favorites_paged` 对称：
    以 ``article_watchlist`` 为主，``LEFT JOIN article_favorites``
    取 ``is_favorite`` 的实际值。每个 item 显式携带 ``is_watchlist=True``。

    ``unread_only=True`` 时仅返回 ``a.read_at = 0`` 的记录，COUNT 同步过滤。
    """
    conn = _get_conn()
    try:
        where = " WHERE a.read_at = 0" if unread_only else ""
        total_row = conn.execute(
            "SELECT COUNT(*) AS cnt "
            "FROM article_watchlist aw "
            "JOIN articles a ON a.id = aw.article_id" + where
        ).fetchone()
        total = total_row["cnt"] if total_row else 0
        offset = (page - 1) * page_size
        rows = conn.execute(
            "SELECT a.*, "
            "       s.nickname AS nickname, "
            "       s.head_img AS head_img, "
            "       (af.article_id IS NOT NULL) AS is_favorite, "
            "       1 AS is_watchlist "
            "FROM article_watchlist aw "
            "JOIN articles a ON a.id = aw.article_id "
            "LEFT JOIN subscriptions s ON s.fakeid = a.fakeid "
            "LEFT JOIN article_favorites af ON af.article_id = aw.article_id "
            + where +
            " ORDER BY aw.created_at DESC "
            "LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            if d.get("nickname") is None:
                d["nickname"] = ""
            if d.get("head_img") is None:
                d["head_img"] = ""
            d["is_favorite"] = bool(d.get("is_favorite", 0))
            d["is_watchlist"] = True
            items.append(d)
        return {"items": items, "total": total}
    finally:
        conn.close()
