# 开源版 RSS 权限隔离修复说明

## 修复时间
2026-05-06

## 修复背景
开源版原先没有区分"轮询器拉取的文章"和"手动获取的历史文章"，导致：
1. 用户手动获取历史文章后，这些历史文章会混入常规 RSS
2. 常规 RSS 和历史 RSS 的内容边界不清晰

## 修复内容

### 1. 数据库变更
为 `articles` 表添加 `source` 字段：
- `source='poll'`: 轮询器自动拉取的文章（常规 RSS 使用）
- `source='deep_fetch'`: 用户手动获取的历史文章（历史 RSS 使用）

**迁移方式**: 自动迁移，启动时会检测并添加字段，已有数据默认标记为 `poll`

### 2. 代码修改

#### 数据层 (`utils/rss_store.py`)
- `save_articles()`: 添加 `source` 参数，默认为 `'poll'`
- `get_regular_articles()`: 只返回 `source='poll'` 的文章
- `get_historical_articles()`: 只返回 `source='deep_fetch'` 的文章
- `count_historical_articles()`: 只统计 `source='deep_fetch'` 的文章
- `get_all_articles()`: 聚合 RSS 只返回 `source='poll'` 的文章
- `get_articles_by_category()`: 分类 RSS 只返回 `source='poll'` 的文章

#### 轮询器 (`utils/rss_poller.py`)
- 保存文章时显式传入 `source='poll'`

#### 历史文章获取 (`routes/admin.py`)
- 保存文章时显式传入 `source='deep_fetch'`

### 3. RSS 输出行为变化

**修复前**：
- 常规 RSS: 返回所有最新文章（包含历史文章）
- 历史 RSS: 返回 `publish_time < created_at` 的文章

**修复后**：
- 常规 RSS (`/api/rss/{fakeid}`): 严格只返回 `source='poll'` 的文章
- 聚合 RSS (`/api/rss/all`): 严格只返回 `source='poll'` 的文章
- 分类 RSS (`/api/rss/category/{id}`): 严格只返回 `source='poll'` 的文章
- 历史 RSS (`/api/rss/{fakeid}/history`): 严格只返回 `source='deep_fetch'` 的文章

## 升级说明

### 自动升级
1. 更新代码到最新版本
2. 重启服务
3. 数据库会自动添加 `source` 字段
4. 已有文章会默认标记为 `source='poll'`

### 手动验证
如果您之前获取过历史文章，需要重新获取以正确标记为 `deep_fetch`：
1. 访问管理页面的"历史文章"功能
2. 重新获取需要的历史文章
3. 新获取的历史文章会正确出现在历史 RSS 中

## 注意事项
1. **已有的"历史文章"会被标记为 `poll`**，如果需要严格分离，建议重新获取历史文章
2. 常规 RSS 可能会出现"断档"（中间日期的文章缺失），这是**预期行为**，因为首次订阅时轮询器只拉取少量文章
3. 如需完整的历史文章，请使用"获取历史文章"功能，并通过历史 RSS 订阅

## 与 SaaS 版的对齐
此次修复与 SaaS 版的权限隔离逻辑保持一致：
- SaaS 版有付费和 `UserHistoricalArticles` 授权表
- 开源版无付费概念，但同样通过 `source` 字段严格分离常规和历史文章
