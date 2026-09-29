# 日报运行与恢复

## 日常任务

GitHub Actions 保留每天 `01:30 UTC` 的定时触发。任务开始即读取 `data` 分支，在独立目录中去重、保存检查点和准备完整日报。只有 ID 集合、AI 结果及 Markdown 全部通过校验的日期才能提交。失败会保留已有日报并返回失败，诊断附件保存 30 天。

元数据直接从分类列表获取；API 客户端依赖已移除。正文解析保留主分类筛选和排除替换稿的规则。访问串行执行，服从 robots.txt（当前为 15 秒）；406/429 连续三次会暂停，并持久化冷却时间。`Retry-After` 超过五分钟时留待后续运行。

Actions 手动运行支持 `daily`、`backfill`、`reindex`，以及不调用 AI、不发布的 `dry_run`。默认分类、模型和语言继续读取仓库变量。站点配置仍使用现有的 `js/data-config.js` 和 `js/auth-config.js`；数据任务不再提交 main 分支。

## 本地运行

先准备最新 `data` 分支的独立 checkout。以下命令只修改该 checkout，不自动推送：

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -v
uv run python -m arxiv_daily.runner daily --data-root /path/to/data-checkout --dry-run
uv run python -m arxiv_daily.runner reindex --data-root /path/to/data-checkout
uv run python -m arxiv_daily.runner backfill --data-root /path/to/data-checkout --manifest recovery/2026-09-24-28.json
```

`./run.sh` 转发相同参数。`daily --date` 只接受当前 UTC 日期，历史恢复必须提供固定 ID 清单。文件名继续使用 UTC 运行日期；显式补抓按清单中的最早失败日期。若当前列表仍包含更早的待补论文，原日期拥有优先权，避免将漏报搬到新日期。

完整运行需要原有 `OPENAI_API_KEY`、`OPENAI_BASE_URL`，以及可选的 `MODEL_NAME`、`LANGUAGE`、`CATEGORIES`、`RESEARCH_PROFILE`/`RESEARCH_PROFILE_PATH`。不应将密钥写入提交文件。

## 状态和结果

- `state/crawl.json`：候选 ID、元数据、逐篇成功 AI 检查点、失败原因和全局冷却时间。AI 缓存包含元数据、模型、语言、提示词、输出结构、研究偏好和服务端点的摘要。
- `run-artifacts/report.json`：各日期期望 ID、缺失项、历史重复项、结果及逐 ID 补抓对账。`prepared` 表示本地完整产物；只有发布步骤成功才表示已推送。
- `state/last-backfill.json`：最近一次补抓的逐 ID 结果。
- `run-artifacts/requests.jsonl`：状态码、非敏感响应头和有限错误正文。已读取的列表 HTML 保存在同一附件目录。
- `assets/file-list.txt`：完整数据目录中的日报 JSONL 文件名；不包含任何状态或临时文件。

发布命令再次验证整组文件，仅提交明确列出的日报、索引和状态文件。推送冲突时停止，不自动覆盖其他提交。加载最新历史后重跑即可；远端未成功保存的状态可从运行附件恢复。

## 9 月 24–28 日恢复

`recovery/2026-09-24-28.json` 包含日志确认的 92 个不同 ID：9 月 24 日 39 个、25 日 25 个、28 日 28 个。先与成功历史记录核对，再处理缺失项；保留 28 日已发布的那篇。26、27 日重复出现的 ID 不另建重复日报。

新部署首次 `daily` 运行会载入该清单并优先恢复更早日期，避免滚动列表把这些论文写入新日期。之后只有未完成项会继续处理；也可手动选择 `backfill` 获取完整对账。

该清单提交到 main 时会触发恢复运行，因此首次修复不必等待延迟的定时调度。普通代码提交不会触发额外抓取，原定时安排保留。

整页无法获取且没有候选 ID 的日期会保留明确的列表缺口。不能用之后的 `/new` 清空这个缺口，也不能用不完整 ID 清单宣称已找齐该日论文；需根据保存的页面或日志建立完整候选范围后修复状态。

## 测试样本

`tests/fixtures` 是 2026-09-29 读取的 arXiv 公开 HTML，当时两个列表的公告日期仍为 2026-09-28。该日期按原筛选范围应解析出 29 个不同 ID。离线测试不请求 arXiv 或 AI。
