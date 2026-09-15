# 架构说明

## 一句话

把人工锁定的剧情事实编译成逐镜头的视觉检查项，让**跨镜头的一致性可以被机械验证**——这是任何单次生成调用都不负责的事。

## 分层

```
app.py                 只做界面与事件绑定，不写任何规则
  ├── orchestrator.py  双模式调度 + Trace（做了什么/多久/模型还是本地/重试几次/为何失败）
  │     ├── creator.py       CREATE：故事理解、镜头规划、canon 装配、约束、prompt
  │     ├── analyzer.py      AUDIT：本地规则、多模态调用、四档决策
  │     └── canon_loader.py  规则编译、锚点、路由、lint、因果审计
  ├── package.py       Production Package 打包
  ├── take_log.py      25 字段 CSV 生产日志
  ├── video_audit.py   上传视频校验 + 5 帧时间序列抽取
  ├── video_provider.py 受控视频生成 Provider（默认关闭）
  └── visual_metrics.py 颜色与像素粗筛
```

依赖是一条直链，没有环，没有第三方 agent 框架。

## 两种模式

### CREATE：一句创意 → Production Package

| 步 | 做什么 | 执行方 |
|---|---|---|
| ① | 故事理解（主题／冲突／主角／基调／场景） | **模型**（无 API 时本地模板） |
| ② | 3—5 镜规划（按建立／升级／转折／代价／收束） | **模型**（无 API 时三幕骨架） |
| ③ | 装配 canon 草案 | **本地确定性** |
| ④ | 生成连续性约束 | **本地确定性** |
| ⑤ | 合成每镜视觉 prompt | **模型**（无 API 时模板） |
| ⑥ | 模型策略（二元路由） | **本地确定性** |
| ⑦ | 草案因果审计（NA-01—NA-08） | **本地确定性** |
| ⑧ | 打包 | 本地 |

**核心设计：模型负责理解与创作，规则负责保证一致性。** ③④⑥⑦ 永远不交给模型——`test_consistency_steps_are_never_delegated_to_a_model` 锁住了这一点。

产出的 `canon_draft.json` 是**真的** canon：`CanonStore` 能载入它，`compile_rule_pack` / `route` / `anchor_for` / `narrative_audit` 全部可用。

### AUDIT：一句指令 + 图片／视频 → 四档决策

| 步 | 做什么 | 执行方 |
|---|---|---|
| ① | 解析指令（镜号 + 问题关键词） | 本地确定性关键词表 |
| ② | 编译该镜规则包 | 本地（canon） |
| ③ | 模型路由 | 本地二元规则 |
| ④ | 视频校验并抽取 5 个时间点（视频输入时） | 本地 `imageio-ffmpeg` |
| ⑤ | Take 审计 | 模型（多模态）或本地规则 |

输出 `PASS` / `LOCAL FIX` / `REGENERATE` / `HUMAN REVIEW`，优先级固定为 `REGENERATE > LOCAL FIX > HUMAN REVIEW`，另有独立的 `needs_human_review` 标志。

## 证据来源：五个明确标签

这是本项目最重要的诚实性约束。**仅凭一句话得出的结论，绝不能看起来像系统看过画面。**

| 标签 | 触发条件 | 含义 |
|---|---|---|
| `USER-REPORTED RULE TRIAGE` | 无图片 | 「若你所述属实，规则判定为此」。系统没看过画面 |
| `RULE + PIXEL CHECK` | 有图，本地规则 | 读了颜色与像素统计，模型未理解画面内容 |
| `VISUAL AUDIT` | 有图 + 多模态成功 | 模型实际查看并逐条核验 |
| `VIDEO FRAME + RULE CHECK` | 有视频，本地规则 | 抽取了有时间标记的帧；形变等语义结论仍来自人工描述 |
| `VIDEO VISUAL AUDIT` | 有视频 + 多模态成功 | 模型查看同一 Take 的有序帧并跨帧核验 |

视频限制为 20 秒／50MB，支持 MP4、MOV、M4V、WebM。抽帧只发生在当前请求内，PIL 帧不写入共享日志或全局状态。多模态请求把 5 帧标成 `VIDEO FRAME 1/5 · 0.00s` 等有序时间点；提示词允许比较可见几何与道具位置，但禁止编造采样点之间的动作或评估音频。

界面在结论上方用横幅明示，并在 `USER-REPORTED` 时额外说明怎样才能升级。

## 重试与降级

**两种"重试"必须分开，代码与界面都分开：**

| | 含义 | 上限 | 是否花钱 |
|---|---|---|---|
| **API 请求重试** | 临时性错误（429／5xx／超时）自动重试 | **2 次** | 不额外产生内容费用 |
| **内容重新生成** | 重新跑一次生成 | 无自动 | **会花钱，必须用户确认** |

错误分类在 `analyzer.is_retryable()`：只有 `429/500/502/503/504/408/409/425` 与网络层异常才重试；**4xx 业务错误一次就停**（重试没有意义）。

重试耗尽后：降级为本地规则结果，`api_error` 写入界面与日志。图片降为 `RULE + PIXEL CHECK`，视频降为 `VIDEO FRAME + RULE CHECK`；决策绝不会冒充由视觉模型支撑的 `PASS`。

## 状态与上下文

| 类别 | 位置 | 生命周期 |
|---|---|---|
| canon | `canon.json`（只读）＋ `canon_history_2026-09-12.json` | 启动时载入 |
| 会话 | `gr.State`（每浏览器会话独立） | 刷新即重置为成片 canon |
| CREATE 草案 | 会话级 `tempfile` 目录 | 不污染 `canon.json`，新会话不可见 |
| 日志 | `data/take_log.csv`（25 字段） | **单进程共享，不区分用户** |

## 安全模式（公开部署的默认状态）

日志共享这一点决定了公开环境必须做三件事，缺一不可：

1. **不渲染原始日志表** —— 界面上只有聚合统计
2. **不绑定回调** —— `refresh_log` 在 `SHOW_RAW_LOGS != "1"` 时根本不进入事件表，因此也不出现在 `/gradio_api/info`，无法被公开 API 调用。仅仅"隐藏组件"是不够的，Gradio 的事件是可以被直接调的
3. **不作为返回值** —— `do_audit()` 从 6 个输出减为 5 个，日志行不再随审计结果返回

另外 `show_error` 绑定到同一个开关：默认关闭，避免 traceback 把容器内绝对路径推到浏览器。

**默认即安全**：忘记配环境变量不会导致泄露；要放开必须显式设置 `SHOW_RAW_LOGS=1`。

`new_session()` 永远返回挂载成片 canon 的干净状态——`test 8` 验证了新会话不会引用他人的临时 canon。

## 日志字段（25 个，v1.2 的 19 个 + 新增 6 个）

新增：`run_mode` `evidence_source` `duration_ms` `retries` `failure_reason` `model_source`

旧版 CSV 在 `TakeLog.__init__` 自动迁移：备份为 `.pre-v1.1`，补齐新列，原记录不丢。

---

# 受控视频生成链路

仓库里有一个真的 MiniMax Hailuo V1 适配器，但**正式公开部署默认关闭**，
公开创空间里既没有生成按钮，也没有任何生成回调进入事件表。

关于"已接入"这件事要说准确：完成了真实视频 API 可行性探测，密钥、网络、端点和请求
结构验证通过，但生成任务在提交阶段因账户套餐权限被拒绝，因此未产生视频，也未接入
正式生成链路。脱敏记录见 `evidence/minimax_feasibility_probe.json`。适配器代码是按
这次探测结论写的，能否真正出片取决于账户权限，不取决于代码。

## 数据通路

每个镜头在 CREATE 阶段就带齐了提交任务所需的结构化字段（`creator._shot`）：

```
shot_id, prompt, duration_sec, aspect_ratio, resolution,
reference_images, model_strategy, status, task_id, video_url,
provider, generation_retries, generation_error
```

`video_provider.task_from_shot(shot, routing)` 把镜头直接变成 `GenerationTask`，
接入适配器时不需要回头改数据结构。

## 状态机

```
AWAITING_EXTERNAL_GENERATION → SUBMITTED → RUNNING → SUCCEEDED / FAILED / HUMAN_REVIEW
SUCCEEDED / FAILED / HUMAN_REVIEW → AWAITING（内容重新生成，需用户二次确认）
```

非法跃迁（如 AWAITING 直接到 SUCCEEDED）由 `can_transition()` 拒绝。

## Provider 映射

| 本地方法 | MiniMax Hailuo V1 |
|---|---|
| `submit(task)` | `POST /v1/video_generation`，立即返回 `task_id`，不阻塞 |
| `poll(task)` | `GET /v1/query/video_generation?task_id=...` |
| `fetch_result(task)` | `GET /v1/files/retrieve?file_id=...`，取得 `download_url` |
| `normalize_error(exc)` | 归一为 transient／entitlement／request／auth／unknown |

Hailuo V1 请求体使用 `prompt`、`first_frame_image`、`duration`、`resolution`，
**不传 `aspect_ratio`**——图生视频的画幅由首帧决定。

**注意不要混用协议**：可行性探测当时试的是 MiniMax-H3-Max，它属于 V2
（`POST /v2/video_generation`，请求体是 `content` 数组），与本适配器走的 Hailuo V1
不是同一套。适配器只实现 V1；换到 H3 系列需要另写一个 Provider 子类。

## 开关与费用边界

Provider 只有同时满足以下条件才可用，缺一即 `get_provider()` 返回 None：

```text
ENABLE_VIDEO_GENERATION=1
VIDEO_PROVIDER=minimax
MINIMAX_VIDEO_API_KEY=<secret>
VIDEO_ACCESS_CODE=<secret>
```

凭据、模型名与端点全部来自环境变量，代码里没有任何硬编码，
`test_credentials_are_never_hardcoded` 与 `test_video_api_key_never_appears_in_any_output` 锁住这一点。

应用内还有三层保护：

1. `MAX_ACTIVE_VIDEO_JOBS=1` —— 防并发重复提交
2. `MAX_DAILY_VIDEO_JOBS=3` —— 限制当日任务数
3. `VIDEO_BUDGET_CNY=10` —— 按已验证价目表估算，超出即拒绝

预算计数在单进程内存里，服务重启会清零，**因此它替代不了账户余额／额度上限**。
测试访问码用 `hmac.compare_digest` 做恒定时间比较，错误访问码在接触 Provider 之前就被拒。

价目表是白名单而不是估算公式：`estimated_cost_cny` 遇到没有验证过价格的
（模型，分辨率，时长）组合会直接抛错拒绝提交，而不是猜一个数字。

## 两种重试，在这里的具体含义

- **POST submit：0 次自动重试。** 提交请求超时的时候，服务端可能已经建了付费任务，
  盲目重发会产生重复费用。
- **GET poll / fetch：最多 2 次。** 只有 429、408、409、425、5xx、超时和网络临时错误才重试。
- 400 参数错误、401 鉴权错误、套餐／余额／权限不足一次就停，转 `FAILED` 或 `HUMAN_REVIEW`。
- 内容重新生成永远需要重新勾选费用确认，没有自动触发路径。

## 首帧约束

`MiniMax-Hailuo-2.3-Fast` 只支持图生视频。提交前在本地验证：JPEG 编码后小于 20MB、
短边大于 300px、宽高比在 2:5 到 5:2 之间。**验证失败不会发出任何网络请求。**

## 时长差值不静默吞掉

平台只接受固定时长（6／10 秒），分镜计划里的秒数不一定在其中。
`prepare_video` 在费用提示里显式写出"计划 N 秒、实际按 M 秒提交"，
把差值交还给剪辑判断，而不是悄悄替换。

## 当前验证状态

- 86 项单元与集成测试通过，其中 MiniMax 与多模态失败路径全部使用 mock，**不产生任何外部请求或费用**
- mock 覆盖：V1 payload 结构、task_id、两次 429 后恢复、file_id、下载地址、
  权限不足不重试、首帧缺失在发请求前拒绝、费用与并发门槛
- Feature Flag 关闭时，Gradio 组件树不包含视频生成面板，且 `prepare_video` /
  `submit_video` / `refresh_video` 均不在事件表里（`test_privacy.py` 断言）
- Feature Flag 与四项凭据齐全时，受控面板可冷启动
- **尚未进行付费真实生成**

---

# 视频审查链路

`gr.Video` 上传 → `video_audit.sample_video()` 校验格式、20 秒和 50MB 上限 → 按 0/25/50/75/100% 五个时间点解码 → Gallery 预览 → `run_audit_chain()` → 本地规则或五帧多模态审查。

Canon 1.4.2 新增 `LOC-B06`：Bay 07 的横向控制台及固定机械组件在同一 Take 内不得熔化、伸缩、替换或重构。它来自两段真实失败素材，而不是为了演示虚构的规则：Shot 19 的控制台整体重构，Shot 21 的拉杆形变；后者还同时命中 `SHOT-21A`（钥匙位置）与 `SHOT-21B`（拉杆运动）。
