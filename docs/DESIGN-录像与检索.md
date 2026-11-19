# 录像存储与视频检索 设计说明

版本：v0.1（2026-11-19）
基于分支：`feat/multi-camera`（多岗位、多摄像头）
相关文档：[架构说明](ARCHITECTURE.md)、[分支说明](BRANCHES.md)、[压测记录](BENCHMARK.md)

## 1. 背景与目标

现在系统只保存每条画面记录的文字和一张小图（关键帧），不保存视频：

- 能回答“上午 9 点发生了什么”。
- 拿不出原视频，也没法按描述快速找到某一段。

本期目标：

1. **录像**：每路摄像头按设定天数保存录像；告警前后的片段长期留证；磁盘快满时自动清理。
2. **检索**：用一句话找到录像片段，定位到“哪一路、哪一秒、画面哪个位置”，点开就能从事发前几秒播放、放大、下载。
3. **入口**：
   - 录像页有搜索框。
   - 值守台可以用语音或文字搜索，结果自动弹出。
   - 钉钉、飞书里能搜，返回结果链接。
4. **可配置**：录像、搜索各自能整块关闭，关掉不影响值守、职责、告警、执行。

不在本期：

- 云台光学变焦（PTZ）：只预留接口。
- 对历史录像逐帧离线重新分析。
- AI 超分辨率：生成的细节不可靠，不适合做证据，明确不做。

## 2. 总体架构

```
写入                                               读取
摄像头 CameraHub ──▶ 录像 Recorder ──▶ VideoStore ◀── 回放 Playback ◀──┐
                                                                        │ 命中：摄像头·时间·框
事件总线（观察记录、告警）──▶ 索引 Indexer ──▶ SearchIndex ◀── 搜索子智能体 SearchAgent
                                                                        ▲
                                         主智能体 search_video ─────────┤
                                         录像页搜索框 /api/search ──────┘
```

解耦原则：

| 原则 | 做法 |
|---|---|
| 写读分离 | 录像、索引只写；回放、检索只读，中间只靠数据。任何一边故障，不影响另一边 |
| 事件通信 | 索引订阅已有的 `TOPIC_OBSERVATION`、`TOPIC_ALERT`；后台观察、告警模块不改 |
| 接口可替换 | `VideoStore`（本地文件 / NVR）、`SearchIndex`（SQLite / 向量）、`PTZ`（ISAPI / ONVIF），用配置选择实现 |
| 依赖向下 | 页面和智能体只认“检索”“回放”两个服务和统一的结果格式（摄像头 + 时间 + 框） |
| 独立开关 | `recording.enabled`、`search.enabled` 各自开关；依赖缺失时降级，不报错 |

代码目录：

```
vva/recording/   Recorder、VideoStore（local.py、nvr.py 预留）、清理与磁盘保护
vva/search/      Indexer、SearchIndex（sqlite.py、vector.py）、SearchAgent、工具、提示词
vva/playback/    取片段、取帧、缩略图、叠框（只读）
vva/ptz/         云台适配器（预留）
vva/web/recording_api.py、search_api.py
vva/web/static/recording.html、search-panel.js
```

## 3. 录像存储

### 3.1 录制

- **转存方式**：每路一个 ffmpeg 进程，`-c copy` 直接转存 RTSP，不重新编码；按 60 秒切片（`-f segment`），写成 MP4。
  ```
  data/recordings/<摄像头>/<YYYY-MM-DD>/<HHMM>.mp4
  ```
- **码流**：
  - 分析固定用子码流。
  - 录像每路可选 `main` / `sub`，默认 `sub`。
  - 需要看清人脸、车牌、工牌的点位用 `main`。
  - 同一台摄像头主、子码流地址不同时，在摄像头设置里另填“录像地址”；不填就录分析用的地址。
- **浏览器推流的那一路**：没有视频流，只有每秒 2 帧的截图。用截图拼成低帧率 MP4（同样按分钟切片），页面标注“低帧率”。
- **进程守护**：ffmpeg 退出后 5 秒自动重启；连续失败时页面显示“录像中断 N 分钟”，超过 `offline_alert_s` 走现有的“摄像头离线”告警。
- **登记**：每写完一片，登记到 `recording_segments` 表（见第 9 节）。检索、回放靠这张表把“摄像头 + 时间”换算成文件和偏移秒数。

### 3.2 保留与清理

按顺序生效：

1. **按天数**：默认 `retention_days: 30`，每路可单独覆盖。每小时检查一次，删除超期的整天目录，同步删除登记记录。
2. **告警片段**：
   - 每条告警（含巡检异常、摄像头离线）生成后，等录满再截取前 60 秒、后 60 秒，存到 `data/recordings/_alerts/<告警编号>.mp4`，和告警记录关联，默认保留 180 天。
   - 普通录像删了，告警证据仍在。
   - 告警被标为“误报”后，片段改按普通天数清理。
3. **磁盘保护**（按录像目录所在磁盘的使用率）：
   - 达到 `clean_at`（默认 85%）：不论天数，从最早的普通录像开始删，删到 `clean_to`（默认 80%）；值守台提示一次。
   - 达到 `stop_at`（默认 95%）：暂停全部录像并告警，保证数据库和日志不会被写满。
   - 告警片段不参与磁盘保护清理，只按自己的天数删。
4. **清理日志**：记到 `recording_cleanups` 表：时间、原因（超期 / 磁盘 / 手动）、摄像头、日期、释放空间。页面可查。

### 3.3 容量估算

| 码流 | 码率（参考） | 每路每天 | 4 路 30 天 |
|---|---|---|---|
| 子码流 720p | 约 1 Mbps | 约 10 GB | 约 1.2 TB |
| 主码流 1080p | 约 4 Mbps | 约 43 GB | 约 5.2 TB |
| 主码流 4K | 约 8 Mbps | 约 86 GB | 约 10 TB |

上线前要实测：现场摄像头的实际码率（`ffprobe` 或录 10 分钟看大小）、`/data7` 的剩余空间。用这两个数确定默认天数和码流。

## 4. 检索索引

### 4.1 写入

`Indexer` 订阅事件总线：

| 来源 | 写成的检索条目 |
|---|---|
| 观察记录（`TOPIC_OBSERVATION`） | 摄像头、时间、一句话描述、人数、物体、动作、异常、关键帧路径 |
| 告警（`TOPIC_ALERT` / 告警表） | 摄像头、时间、职责名称、依据、目标框、截图、告警片段 |
| 巡检记录 | 摄像头、时间、巡检结果 |

- 长期记忆里已有的历史记录（`events` 表，已带摄像头列）首次启动时导入一次，上线当天就能搜到之前的记录。
- `search.keep_indexing: true` 时，即使关掉搜索功能也继续建索引，以后打开就能搜到这段时间。

### 4.2 查询

`SearchIndex.query(Query) -> list[Hit]`：

- **条件**：摄像头、岗位、时间段、类型（记录 / 告警 / 巡检）、人数范围、职责。
- **关键词**：SQLite FTS5 全文检索，中文按字二元切分。
- **语义（可选）**：`vector: true` 时，描述文字和关键帧用向量模型编码，按相似度召回，再与条件结果合并。向量模型很小，部署在 GPU2 / GPU3，不占 0、1 号卡。

### 4.3 精度说明

- 后台观察约每 5～30 秒记一次，检索命中点与事件真正开始的时刻可能差几秒到几十秒。播放时默认从命中点前 10 秒开始；需要时由子智能体逐秒精确定位（见 5.3）。
- 一闪而过、没被观察记录到的事件搜不到。这是“按记录检索”的固有限制，在页面帮助里说明。

## 5. 搜索子智能体

### 5.1 对外约定

主智能体只看到一个工具：

```
search_video(question, camera?, start?, end?)
```

返回格式（主智能体、录像页、钉钉都只认这个）：

```json
{
  "search_id": "s20261119-0007",
  "found": true,
  "summary": "昨天 12:00～18:00 在 1 号仓库共找到 2 次有人吸烟",
  "scope": {"cameras": ["cam0", "cam1"], "start": "...", "end": "..."},
  "hits": [
    {"camera": "cam0", "camera_label": "1 号仓库·大门", "start": "2026-11-18 14:23:05", "end": "14:24:10",
     "box": [0.21, 0.33, 0.42, 0.88], "evidence": "左侧穿灰衣男子手持点燃的香烟",
     "thumb": "/api/search/s20261119-0007/thumb/0", "clip": "/api/playback/clip?cam=cam0&start=...&end=...",
     "alert_id": 1234, "confidence": "高"}
  ],
  "note": "只确认了前 16 个候选"
}
```

### 5.2 内部

- 复用现有 `Agent` 类，配专用提示词和专用工具。每次调用用一个临时会话，用完丢弃，不占主智能体上下文。
- 内部工具：

| 工具 | 作用 |
|---|---|
| `query_records` | 按条件、关键词、语义查检索条目（粗筛） |
| `look_frames` | 看几张候选关键帧，确认是否符合描述（排除误报） |
| `refine_time` | 在命中点前后 30 秒的录像里按每秒 1 帧找起始时刻 |
| `locate` | 在命中帧上框出目标（复用值守台的定位能力） |
| `make_result` | 生成片段地址、缩略图，写入 `search_runs`，返回结果 |

- **预算**：`max_rounds`（工具调用轮数）、`max_frames`（最多看多少帧）、`timeout_s`。超出时带着已确认的结果返回，并在 `note` 里说明。
- **时间解析**：“昨天下午”“上周三”“刚才”按服务器时区解析成具体时间段，结果里回显，避免误解。
- **范围默认值**：
  - 值守台发起：默认当前正在看的那一路所在的岗位，可以说“所有摄像头”扩大范围。
  - 钉钉发起：默认全部摄像头。
  - 都不指定时间：默认最近 24 小时。

### 5.3 与现有系统的配合

| 事项 | 做法 |
|---|---|
| 模型 | 默认沿用主模型（Qwen3.6-35B-A3B，“大脑”和“看图”同一服务）；`search.llm` / `search.vlm` 可单独指向别的模型 |
| 算力排队 | 看图请求走视觉调度器，新增优先级 `SEARCH`，排在用户提问、告警复核之后，职责检查之前 |
| 进度 | 发 `search_progress` 事件（正在查哪段、候选几个、确认到第几个），值守台、录像页实时显示 |
| 取消 | 跟随主智能体这一轮一起取消；录像页可点“取消”，调用取消接口 |
| 权限 | 搜索只读，钉钉、飞书普通成员可用；下载片段、导出需要管理口令 |
| 降级 | 没开录像：只给关键帧和时间，提示“没有录像可播放”；没配向量模型：退回关键词；排队超时：返回已有结果 |

### 5.4 开关

```yaml
search:
  enabled: true
  keep_indexing: true
  vector: false
  verify_frames: true
  refine_time: true
  channels: [web, dingtalk, feishu]
  budget: {max_rounds: 6, max_frames: 16, timeout_s: 30}
  # llm: {...}  vlm: {...}   不写则沿用 models 里的主模型
```

- `enabled: false`：主智能体看不到 `search_video`，录像页隐藏搜索框，接口返回“未开启”。
- 网页“设置”里有运行时开关（存 `ops.db`，覆盖配置文件），不用重启，下一句话生效。

## 6. 回放与放大

- **取片段**：`/api/playback/clip?cam=&start=&end=`。按 `recording_segments` 找到覆盖时间段的切片，用 ffmpeg 拼接、裁剪（`-c copy`），输出 MP4，缓存 1 小时。片段最长 10 分钟。
- **取帧**：`/api/playback/frame?cam=&t=` 返回指定秒的 JPEG（子智能体逐秒定位、缩略图都用它）。
- **播放器**（`search-panel.js`，录像页和值守台共用）：
  - 默认从命中点前 10 秒开始，可前后拖动。
  - 目标框按时间叠加在画面上，点框自动放大到该区域，也支持滚轮缩放、拖动。
  - 截图、下载片段（需要管理口令）。
- **清晰度**：放大是数字放大，能看多清楚取决于录像码流；关键点位建议录主码流，或至少保证告警片段用主码流。
- **云台（预留）**：`PTZ` 接口加海康 ISAPI / ONVIF 适配器，作为“执行”里的动作（“镜头拉近门口”），或告警时自动对准目标抓拍特写。需先确认现场摄像头是否带云台。

## 7. 页面

### 7.1 录像页（顶部导航新增“录像”）

自上而下：

1. **搜索框**：一句话、可选摄像头、时间范围；搜索中显示进度，可取消；下方列出结果卡片和播放器；“搜索历史”可重新打开。
2. **指标**：
   - 磁盘已用 / 总量（进度条）。
   - 录像占用和每天新增量。
   - “按现在的速度还能存 N 天”：少于设定天数时标红。
   - 告警片段的数量和占用。
3. **每路一行**：录像码流、今日大小、最早录像、保留天数、状态（录像中 / 中断 N 分钟 / 已停用 / 低帧率）。展开是按天的时间轴：有录像的时段为实条，缺口一目了然，点任意位置回放。
4. **保留设置**：
   - 可改默认天数、单路天数、告警片段天数、磁盘阈值。
   - 保存前估算所需空间（如“改成 60 天约需 2.6 TB”）。
   - 需要管理口令。
5. **清理记录**：最近的清理时间、原因、释放空间。

### 7.2 值守台语音 / 文字搜索

1. 用户说“昨天下午仓库有没有人抽烟”→ 主智能体调用 `search_video`。
2. 指令区显示进度行：“检索录像：昨天 12:00–18:00 · 1 号仓库”“找到 5 个候选，正在确认…”。
3. 结果返回后发 `search_results` 事件，**右侧自动弹出结果面板**：
   - 覆盖告警区，不挡中间的实时画面。
   - 第一条自动选中并播放。
4. 主智能体用一句话语音回答：“找到 2 次，已在右边打开：14:23 大门、16:41 库房。”
5. 后续可以用语音操作结果：“放大看第二个”“播放 14:23 那段”“下载下来”。对应主智能体工具 `play_hit`、`zoom_hit`，只在有打开的结果时出现。
6. 面板可关闭，或点“在录像页打开”跳转（`/recording?search=<编号>`）。

规则：

- 没找到时不弹面板，只语音说明搜了哪个范围，并建议扩大范围。
- 结果事件带会话编号，只弹给发起搜索的那个值守台。
- 搜索途中被打断（“算了”或新问题）就取消，不弹面板。

### 7.3 钉钉、飞书

回一张消息卡片：摘要 + 每条命中的时间、摄像头、缩略图 + 链接（打开录像页并定位到这次搜索）。

## 8. 事件

| 事件 | 发送方 | 接收方 | 内容 |
|---|---|---|---|
| `TOPIC_OBSERVATION`、`TOPIC_ALERT` | 已有 | Indexer（新增订阅） | 不变 |
| `recording_status` | Recorder | 值守台、录像页 | 每路录像状态变化 |
| `storage_warning` | Recorder | 值守台 | 磁盘达到清理线或停止线 |
| `search_progress` | SearchAgent | 发起会话 | 搜索编号、阶段说明、进度 |
| `search_results` | SearchAgent | 发起会话 | 完整结果（第 5.1 节格式） |

## 9. 数据表（`data/ops.db`）

```sql
CREATE TABLE recording_segments (
  id INTEGER PRIMARY KEY, camera TEXT, start REAL, end REAL, path TEXT,
  bytes INTEGER, stream TEXT, low_fps INTEGER DEFAULT 0);
CREATE INDEX idx_seg ON recording_segments(camera, start);

CREATE TABLE alert_clips (alert_id INTEGER PRIMARY KEY, camera TEXT, start REAL, end REAL,
  path TEXT, bytes INTEGER, keep_until REAL);

CREATE TABLE recording_cleanups (id INTEGER PRIMARY KEY, ts REAL, reason TEXT,
  camera TEXT, day TEXT, freed_bytes INTEGER);

CREATE TABLE search_entries (id INTEGER PRIMARY KEY, camera TEXT, ts REAL, kind TEXT,
  text TEXT, people INTEGER, frame TEXT, box TEXT, alert_id INTEGER, duty_id TEXT);
CREATE VIRTUAL TABLE search_fts USING fts5(text, content='search_entries', content_rowid='id');

CREATE TABLE search_runs (id TEXT PRIMARY KEY, ts REAL, session TEXT, question TEXT,
  scope TEXT, result TEXT, status TEXT);
```

向量检索打开时，另建 `search_vectors`（条目编号 → 向量），数据量大后可换成独立的向量库，只需新增一个 `SearchIndex` 实现。

## 10. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/recording/overview` | 指标、每路状态 |
| GET | `/api/recording/timeline?cam=&day=` | 某路某天的录像时段 |
| GET / PUT | `/api/recording/settings` | 保留设置（PUT 需管理口令） |
| GET | `/api/recording/cleanups` | 清理记录 |
| GET | `/api/playback/clip?cam=&start=&end=` | 片段 MP4 |
| GET | `/api/playback/frame?cam=&t=` | 指定秒的画面 |
| GET | `/api/alerts/{id}/clip` | 告警片段 |
| POST | `/api/search` | 发起搜索 `{question, camera?, start?, end?}`，返回搜索编号；进度和结果走 WebSocket |
| GET | `/api/search/{id}` | 搜索结果 |
| POST | `/api/search/{id}/cancel` | 取消 |
| GET | `/api/search/history` | 搜索历史 |
| GET | `/recording` | 录像页 |

## 11. 配置汇总

```yaml
recording:
  enabled: true
  dir: data/recordings
  segment_s: 60
  stream: sub                       # 默认录像码流；每路可在摄像头设置里覆盖
  retention_days: 30                # 默认天数；每路可覆盖
  alert_clip: {before_s: 60, after_s: 60, retention_days: 180}
  disk_guard: {clean_at: 85, clean_to: 80, stop_at: 95}
  store: local                      # local | nvr（预留）

search:                             # 见第 5.4 节
  enabled: true
  keep_indexing: true
  vector: false
  verify_frames: true
  refine_time: true
  channels: [web, dingtalk, feishu]
  budget: {max_rounds: 6, max_frames: 16, timeout_s: 30}
```

## 12. 测试与验收

单元测试（假模型、假 ffmpeg、临时目录）：

- 切片登记、按时间找文件和偏移。
- 超期清理、磁盘保护的删除顺序；告警片段不被磁盘保护删除。
- 索引写入、条件和关键词查询、导入历史记录。
- 子智能体：预算截断、取消、降级（无录像、无向量）、结果格式。
- 视觉调度器新优先级的排队顺序。
- 接口：权限（下载、设置需口令）、搜索编号打开结果。

服务器验收：

| 项目 | 标准 |
|---|---|
| 录像连续性 | 4 路连续录 24 小时，缺口只出现在摄像头真实断开期间 |
| 码率与容量 | 实测每路每天大小，与页面显示的“还能存 N 天”误差在 10% 以内 |
| 检索速度 | 条件 / 关键词检索 1 秒内出候选；带看图确认的完整搜索在 30 秒预算内完成 |
| 定位精度 | 对已知事件（现场演示吸烟 10 次），命中率与播放起点误差（目标 ≤ 5 秒）记录在案 |
| 对值守的影响 | 搜索期间重新压测（`scripts/bench_vlm.py`），告警复核 P95 延迟不超过 4 秒 |
| 语音流程 | 值守台说一句话 → 面板弹出并播放 → 语音“放大看第二个”生效 |

## 13. 风险与待定

| 项 | 说明 | 处理 |
|---|---|---|
| 存储容量 | 主码流 30 天需 5～10 TB | 上线前实测码率和 `/data7` 剩余空间，再定默认码流和天数 |
| 检索漏检 | 依赖观察记录，一闪而过的事件可能搜不到 | 页面说明；关键行为改用职责盯守（会出告警，必有片段） |
| 浏览器推流路 | 只有每秒 2 帧 | 标注“低帧率”；正式点位用 RTSP |
| 搜索占算力 | 看图确认、逐秒定位都要用视觉模型 | 独立优先级 + 预算；上线后压测验证 |
| 现场已有 NVR | 可能不需要自己录 | `VideoStore` 预留 NVR 实现，按现场情况选 |
| 隐私合规 | 录像保存、查看、导出 | 保留天数、谁能下载按单位规定配置；导出记日志 |
| 云台 | 依赖摄像头型号 | 本期只留接口，确认型号后再做 |

## 14. 实施计划

新建分支 `feat/recording-search`（基于 `feat/multi-camera`），分四步，每步可单独测试、上线：

| 步骤 | 内容 | 产出 |
|---|---|---|
| 1 录像 | Recorder、切片登记、清理、磁盘保护、告警片段、回放接口、录像页（指标、每路状态、时间轴、设置） | 能录、能看、能自动清理 |
| 2 索引 | Indexer、SQLite 全文检索、历史导入、`/api/search` 的条件和关键词检索 | 录像页能按条件和关键词搜 |
| 3 子智能体 | SearchAgent、内部工具、预算、调度优先级、进度和取消；主智能体 `search_video` | 一句话搜索，带看图确认和精确定位 |
| 4 交互 | 结果面板组件、值守台语音弹窗、`play_hit` / `zoom_hit`、钉钉卡片、搜索历史 | 语音搜索、自动弹出结果 |

上线前需要现场提供：摄像头主、子码流地址和实测码率；`/data7` 剩余空间；录像保留天数和导出权限的规定；是否有 NVR；摄像头是否带云台。
