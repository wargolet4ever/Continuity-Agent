# 最终测试报告

## A. 单元与集成测试（101 项，全部通过）

```
运行时间：2026-09-14T12:18:04+00:00
test_agent.AgentTests  —— 27 项
   ✓ test_after_does_not_inherit_observations
   ✓ test_api_success_empty_issues_pass
   ✓ test_batch_api_rank_and_failure
   ✓ test_bay07_temporal_geometry_deformation_requires_regeneration
   ✓ test_binary_routing_and_overrides
   ✓ test_canon_lint_resolved
   ✓ test_four_component_data_and_compiler
   ✓ test_invalid_api_responses_never_pass
   ✓ test_local_scope_and_shot16_performance
   ✓ test_log_statistics_and_migration
   ✓ test_low_confidence_review
   ✓ test_manual_pass
   ✓ test_narrative_anchor_and_memory_checks
   ✓ test_narrative_audit_clean_on_finished_cut
   ✓ test_narrative_audit_reproduces_production_finding
   ✓ test_no_current_image_never_api_pass
   ✓ test_note_exposed
   ✓ test_priority_and_review_flag
   ✓ test_prompt_preserves_traversal
   ✓ test_red_alert
   ✓ test_scope
   ✓ test_shot_19_small_screen_is_local_fix
   ✓ test_shot_21_wrong_key_requires_regeneration
   ✓ test_skip_does_not_call_any_checker
   ✓ test_small_screen_with_similar_previous
   ✓ test_take_log
   ✓ test_warm_colors_not_red

test_orchestrator.AuditTests  —— 4 项
   ✓ test_clean_flag_is_dropped_when_a_problem_is_also_mentioned
   ✓ test_natural_language_reaches_all_four_decisions
   ✓ test_trace_has_four_steps
   ✓ test_unknown_shot_falls_back_without_crashing

test_orchestrator.CreateTests  —— 7 项
   ✓ test_all_steps_ran_and_succeeded
   ✓ test_consistency_steps_are_never_delegated_to_a_model
   ✓ test_draft_canon_is_consumable_by_canonstore
   ✓ test_empty_idea_rejected
   ✓ test_every_shot_has_a_prompt_and_awaits_generation
   ✓ test_package_contents
   ✓ test_shot_count_and_clamping

test_orchestrator.IntentTests  —— 1 项
   ✓ test_classify

test_orchestrator.MultimodalRetryTests  —— 4 项
   ✓ test_400_is_not_retried
   ✓ test_429_retries_twice_then_degrades
   ✓ test_degraded_audit_is_logged_with_retries_and_reason
   ✓ test_timeout_retries_then_degrades

test_orchestrator.RetryPolicyTests  —— 2 项
   ✓ test_api_retry_limit_is_two
   ✓ test_no_model_caller_without_credentials

test_orchestrator.VideoExtensionPointTests  —— 10 项
   ✓ test_cost_table_and_guard
   ✓ test_credentials_are_never_hardcoded
   ✓ test_fast_model_requires_first_frame_and_never_calls_network
   ✓ test_no_provider_is_available
   ✓ test_poll_retries_two_429s_then_succeeds_and_fetches_url
   ✓ test_provider_requires_explicit_feature_flag
   ✓ test_shot_carries_everything_needed_to_submit
   ✓ test_status_machine
   ✓ test_submit_permission_error_is_not_retried
   ✓ test_submit_uses_v1_payload_and_returns_task_id

test_privacy.PrivateModeTests  —— 1 项
   ✓ test_raw_logs_available_when_explicitly_enabled

test_privacy.PublicModePrivacyTests  —— 12 项
   ✓ test_aggregate_stats_contain_no_free_text
   ✓ test_audit_response_does_not_carry_log_rows
   ✓ test_default_is_safe
   ✓ test_disabled_video_callbacks_refuse_even_if_called_directly
   ✓ test_no_public_endpoint_returns_log_rows
   ✓ test_refresh_log_is_not_bound_to_any_event
   ✓ test_refresh_log_returns_nothing_in_public_mode
   ✓ test_server_side_logging_still_happens
   ✓ test_session_b_cannot_read_session_a_via_ui
   ✓ test_show_error_is_off_in_public_mode
   ✓ test_video_api_key_never_appears_in_any_output
   ✓ test_video_provider_is_off_and_no_generation_callback_is_bound

test_ui.UITests  —— 11 项
   ✓ test_audit_auto_logs_with_new_fields
   ✓ test_audit_with_image_is_not_user_reported
   ✓ test_audit_without_image_is_labelled_user_reported
   ✓ test_create_mounts_draft_canon_into_session
   ✓ test_create_rejects_empty_idea
   ✓ test_demo_buttons_run_audit_in_one_click
   ✓ test_demo_does_not_write_production_log
   ✓ test_empty_create_survives_gradio_postprocessing
   ✓ test_health
   ✓ test_narrative_states
   ✓ test_routes

test_video_audit.VideoAuditTests  —— 7 项
   ✓ test_bundled_examples_extract_five_ordered_frames
   ✓ test_image_and_video_together_are_rejected_before_logging
   ✓ test_multimodal_payload_contains_ordered_video_frames
   ✓ test_preview_returns_captioned_gallery
   ✓ test_shot19_video_audit_uses_real_error_and_logs_filename
   ✓ test_shot21_video_routes_all_three_observed_failures
   ✓ test_video_multimodal_429_retries_twice_then_degrades

Ran 86 tests in 9.247s
OK
```

MiniMax 视频生成相关的 10 项、多模态重试相关的 5 项均使用 mock，
**不产生任何外部网络请求或费用**。

2026-09-14 线上运行日志曾记录：空 CREATE 的错误分支把 `""` 返回给
`Dataframe`，Gradio 因而把它当作 CSV 路径并抛出 `FileNotFoundError`。现已改为
空行列表 `[]`；新增的 `test_empty_create_survives_gradio_postprocessing` 会完整经过
Gradio `process_api` 后处理，而不只直接调用 Python 函数。

## B. 全新环境部署验证（14/14 通过）

在干净的 `venv` 中从 `requirements.txt` 安装依赖，**全程无任何 API Key**：

```
[PASS] 1  冷启动：模块导入 + Blocks 构建          1.702s
[PASS] 2  无 Key 时 get_provider() 返回 None
[PASS] 3  无 Key 时 CREATE 完整降级运行           7 步全过，4 镜
[PASS] 4  降级已在界面如实标注
[PASS] 5  无 Key 时 AUDIT 标明证据等级            USER-REPORTED RULE TRIAGE
[PASS] 6  上传图片后审计不崩溃                    升级为 RULE + PIXEL CHECK
[PASS] 7  上传 Shot 19 视频审查                    5 帧 / 0.826s / REGENERATE / LOC-B06
[PASS] 8  上传 Shot 21 视频审查                    5 帧 / 0.814s / REGENERATE / 3 条规则
[PASS] 9  视频预览                                 5 个有序时间点，画廊正常
[PASS] 10 Production Package 可下载可解压         7 文件，zip 完整性校验通过
[PASS] 11 旧版 CSV 自动迁移                       原记录保留，补齐新列，备份为 .pre-v1.1
[PASS] 12 新会话不会引用他人临时 canon            新会话挂载 成片 canon v1.4.2
[PASS] 13 Gradio 路由                              /、/config、/gradio_api/info 均为 200
[PASS] 14 付费 API 调用                            0 次
```

注：日志可观测性新增字段的真实数量是 **6 个**：`run_mode`、
`evidence_source`、`duration_ms`、`retries`、`failure_reason`、`model_source`。
代码、README、架构说明与本报告均已统一为 6 个。

第一次冷启动曾被测试机自带的 SOCKS 代理变量阻挡，因为干净环境没有安装可选的
`socksio`；清除与产品无关的代理变量后冷启动通过。ModelScope 部署不依赖该代理。

## C. 多模态重试路径（4 项，用 mock 证明）

这一组专门验证 **AUDIT 的真实多模态调用**，不是 CREATE 的文本模型调用：

| 测试 | 构造 | 断言 |
|---|---|---|
| `test_429_retries_twice_then_degrades` | mock HTTP 429 | `api_retries == 2`、`api_reviewed == False`、错误含「已重试 2 次仍失败」、证据等级不为 VISUAL AUDIT、轨迹末步 `retries == 2` 且含「已降级为本地规则」 |
| `test_timeout_retries_then_degrades` | mock `TimeoutError` | `api_retries == 2`，降级成功 |
| `test_400_is_not_retried` | mock HTTP 400 | `api_retries == 0`，错误含「未重试」——业务错误重试没有意义 |
| `test_degraded_audit_is_logged_with_retries_and_reason` | 429 + 真实走 `app.do_audit` | CSV 落库 `retries == "2"`、`failure_reason` 含 429、`model_source == "test-vision"`、`evidence_source == RULE + PIXEL CHECK` |

另有视频专用测试 `test_video_multimodal_429_retries_twice_then_degrades`：把 Shot 21
抽出的 5 帧送入真实多模态请求组装路径，连续模拟 429，断言最多重试 2 次，随后降级为
`VIDEO FRAME + RULE CHECK`，不会误标成模型已经看过视频。

## C1. 直接上传视频审查（7 项）

- 支持 MP4 / MOV / M4V / WebM，单段不超过 20 秒、50 MB。
- 服务端均匀抽取 5 个有序时间点；原视频不复制进全局状态，日志只保存文件名。
- Shot 19 的真实样例判为 `REGENERATE`，命中 `LOC-B06`（操纵台时序几何变形）。
- Shot 21 的真实样例判为 `REGENERATE`，同时命中 `LOC-B06`、`SHOT-21A`、`SHOT-21B`。
- 同时上传图片与视频会在写日志前拒绝，避免证据来源混淆。
- 无模型 Key 时证据标签是 `VIDEO FRAME + RULE CHECK`；只有远端多模态模型成功返回时
  才能升级为 `VIDEO VISUAL AUDIT`。

## C2. 公开环境隐私隔离（12 项，`test_privacy.py`）

**背景**：`take_log` 是全进程共享的一份 CSV，不区分用户。外部检查发现公开页面可通过
「技术细节 → 生产日志」或 Gradio 接口读到全部使用者的 prompt、备注与文件名。

**修复**（默认即安全，不需要任何配置）：

| 措施 | 实现 |
|---|---|
| 不渲染原始日志表 | 公开模式下 `log_table` 组件根本不创建，只保留聚合统计 |
| 不绑定回调 | `refresh_log` 在 `SHOW_RAW_LOGS != "1"` 时不进入事件表 —— 因此不出现在 `/gradio_api/info`，无法被公开 API 调用。**仅隐藏组件是不够的，Gradio 事件可以被直接调** |
| 不作为返回值 | `do_audit()` 输出从 6 项减为 5 项，日志行不再随审计结果返回 |
| 关闭 show_error | 绑定到同一开关，避免 traceback 暴露容器内绝对路径 |
| 服务端继续记录 | 日志功能本身未削弱，只是不对公开访客展示 |

**双会话测试**：会话 A 提交含随机 UUID 的 prompt / 备注 / 文件名；会话 B 走遍
`do_audit` / `refresh_log` / `stats` / `run_demo` / `do_create` / `shot_detail` /
`canon_health` / `narrative_health` 八个回调，再拉 `/config` 与 `/gradio_api/info`。
**三个秘密串在所有输出中均未出现。**

另有一项反向测试：`SHOW_RAW_LOGS=1` 时原始行确实可读 —— 证明这是开关，不是把功能删了。

**视频生成的同一条逻辑**（本轮新增 2 项）：未设置 `ENABLE_VIDEO_GENERATION` 时，
`prepare_video` / `submit_video` / `refresh_video` 都不在 `demo.fns` 里，因此不可能被公开
API 调到；即使有人直接拿到函数引用调用，Provider 门也会先拒绝。另有一项检查确认
`MINIMAX_VIDEO_API_KEY` 只出现在 `os.environ` 与 `required_env` 的读取处，没有任何硬编码。

## C3. 受控视频生成链路（10 项，全部 mock）

| 测试 | 构造 | 断言 |
|---|---|---|
| `test_no_provider_is_available` | 只设 `VIDEO_PROVIDER`，不设 Feature Flag | `get_provider()` 为 None |
| `test_provider_requires_explicit_feature_flag` | Flag + Key + 访问码齐全 | 返回 `MiniMaxHailuoProvider` |
| `test_submit_uses_v1_payload_and_returns_task_id` | mock 成功响应 | 打到 `/v1/video_generation`；请求体有 `first_frame_image`、无 `aspect_ratio`；`task_id` 落位，状态 `SUBMITTED` |
| `test_fast_model_requires_first_frame_and_never_calls_network` | Fast 模型 + 无首帧 | `urlopen` **一次都没被调用**，状态 `HUMAN_REVIEW` |
| `test_poll_retries_two_429s_then_succeeds_and_fetches_url` | 429 → 429 → Success → file | `retries == 2`，共 4 次请求，拿到 `download_url` |
| `test_submit_permission_error_is_not_retried` | HTTP 400 / 权限不足 | 只请求 1 次，归一为 `ENTITLEMENT`，`retryable` 为 False |
| `test_cost_table_and_guard` | 并发 1、当日 1、预算 2 元 | 第二次预约抛错；释放后仍因当日上限抛错 |
| `test_credentials_are_never_hardcoded` | 扫源码 | 凭据全部 `os.getenv`，无硬编码 |
| `test_status_machine` | 非法跃迁 | `can_transition` 拒绝 |
| `test_shot_carries_everything_needed_to_submit` | CREATE 产出的镜头 | 能直接变成 `GenerationTask` |

**关于"提交超时不重试"**：POST 没有自动重试路径，这是刻意的——提交超时时服务端
可能已经建了付费任务，重发会产生重复费用。只有幂等的 GET 才重试。

## C5. 缺可选依赖时的降级（5 项，`test_no_ffmpeg.py`）

**场景**：有人 clone 下来忘了 `pip install -r requirements.txt`。

**原来的行为**：`video_audit.py` 顶层无条件 `import imageio_ffmpeg`，于是
`ModuleNotFoundError` 一路冒到 `app.py` 的导入，**整个应用起不来**——包括跟视频
毫无关系的图片审查。

**修复**：try/except 导入，置 `VIDEO_SUPPORTED` 标志位。缺依赖时：
视频控件不渲染、`preview_video` / `demo_media` 不进事件表、演示按钮退回纯文字审查
（证据等级自动降到 `USER-REPORTED RULE TRIAGE`）、图片审查完全不受影响。

| 测试 | 断言 |
|---|---|
| `test_app_imports_without_ffmpeg` | `VIDEO_SUPPORTED` 为 False，`demo` 仍然构建成功 |
| `test_image_audit_still_works` | 图片审查照常拿到 `RULE + PIXEL CHECK` |
| `test_demo_buttons_degrade_instead_of_crashing` | 演示按钮返回 `REGENERATE` + `USER-REPORTED`，并说明原因 |
| `test_no_video_callback_is_bound` | `preview_video` / `demo_media` 都不在事件表里 |
| `test_sample_video_reports_the_real_reason` | 直接调用抛的是人话，不是 `NameError` |

测试用屏蔽 `sys.modules` 的方式模拟；另外在全新虚拟环境里**真的卸载了
`imageio-ffmpeg`** 验证过一遍，结果一致，装回后视频功能恢复。

## C6. 系统代理下的启动失败（3 项 + 一次真实复现）

**用户报的现象**（Windows）：

```
Exception: Couldn't start the app because
'http://localhost:7860/gradio_api/startup-events' failed (code 503).
```

**根因**：Gradio 在 `blocks.py:2760` 用 `httpx.get()` 对自己做一次启动自检。
裸的 `httpx.get` 默认 `trust_env=True`，会读环境里的 `HTTP(S)_PROXY`——
于是这个**本机回环**请求也被塞进了代理。装了科学上网工具或在公司网络里的机器
几乎必然命中。报错只说「检查网络或代理设置」，很难联想到是本机回环被代理了。

**修复**：`bypass_proxy_for_localhost()` 把回环地址追加进 `NO_PROXY` / `no_proxy`
（追加，不覆盖用户已有配置），在 `launch()` 之前调用。

**真实复现**（清空容器预设的 NO_PROXY，把代理指向一个死端口）：

| | 结果 |
|---|---|
| 关掉修复 | `ValueError: When localhost is not accessible, a shareable link must be created` —— 启动失败 |
| 开着修复 | 正常启动，`curl` 返回 `HTTP 200` |

**一个顺带的发现**：失败那次也走了「改用 127.0.0.1 重试」的兜底分支，**仍然失败**。
说明回环兜底只对「绑定地址不通」有用，对「代理拦截回环」无效——两个原因要分开治。
兜底保留，但不夸大它的作用。

| 测试 | 断言 |
|---|---|
| `test_loopback_is_added_when_unset` | 没配过 NO_PROXY 时补上 localhost / 127.0.0.1 |
| `test_existing_entries_are_kept_not_clobbered` | 用户自己配的 `internal.corp` 等条目不被冲掉 |
| `test_running_twice_does_not_duplicate` | 重复调用不会把 localhost 写两遍 |

端到端那一条没有做成自动化测试：它需要起子进程改环境变量，单次约 20 秒，
而整个套件现在跑 13 秒。上面的手工复现记录代替它。

## C7. ffmpeg 二进制不可用（3 项 + 真实复现）

**用户报的现象**（Windows）：连**图片**审查都抛
`FileNotFoundError: [WinError 2] The system cannot find the file specified`。

**根因**：`VIDEO_SUPPORTED` 旧逻辑只检查 `import imageio_ffmpeg` 成不成功，
**没检查二进制到底在不在**。包能导入不代表 exe 存在——杀毒软件会删它；而
`IMAGEIO_FFMPEG_EXE` 这个环境变量被 imageio-ffmpeg **直接信任、根本不校验**
（源码注释原话：`Dont test it: the user is explicit here!`），指错路径就会一路
带到真正抽帧时才炸。于是应用自以为视频可用，渲染了视频控件、绑定了回调，
炸点却出现在用户看来毫不相干的地方。

**修复**：`_probe_ffmpeg()` 在导入时**真的把二进制跑一次** `ffmpeg -version`，
三种情况都判为不可用——包缺失、路径解析失败、跑起来报错。可用时把路径存进
`FFMPEG_EXE`，抽帧直接用它，不再每次重新解析。另外每条 ffmpeg 调用路径单独
捕获 `FileNotFoundError`，给一句人话而不是 traceback。

**真实复现**（`IMAGEIO_FFMPEG_EXE=/nonexistent/ffmpeg.exe`，等价于她的 WinError 2）：

| | 旧逻辑 | 修复后 |
|---|---|---|
| `VIDEO_SUPPORTED` | True（只看 import） | **False** |
| 报错形态 | `FileNotFoundError` traceback | 「ffmpeg 找到了但跑不起来…诊断：python diagnose.py」 |
| 图片审查 | 受牵连 | **照常可用** |
| 演示按钮 | 崩 | **照常可点**（退回纯文字） |
| 视频回调 | 已绑定 | **不进事件表** |

| 测试 | 断言 |
|---|---|
| `test_bad_exe_path_is_caught_at_import_not_at_first_use` | 坏路径在导入期就判不可用，且抛人话不抛 WinError |
| `test_working_ffmpeg_records_the_resolved_path` | 可用时 `FFMPEG_EXE` 被记下且真能跑 |
| `test_nonzero_exit_code_also_degrades` | 二进制在但返回非 0，同样判不可用 |

另外新增 `diagnose.py`：一条命令查清包在不在、路径解析到哪、文件存不存在、
真跑一次会怎样，并给出对应修法。

## D. 安全检查

```
仓库硬编码密钥扫描        无命中（sk-* / Bearer * / 各类 *_API_KEY = "..."）
evidence/ 脱敏终检        无残留（API Key / Authorization / 计费方案名 / 账号标识）
video_provider.py         无 sk-，凭据全部 os.getenv，有两项测试锁定
MiniMax 适配器            Key 只从 MINIMAX_VIDEO_API_KEY 读；访问码用 hmac.compare_digest 恒定时间比较
首帧 base64               任务建立后即从会话状态清除，不长期留存
git 提交历史              已扫描所有可达提交，高置信密钥模式 0 命中
样例视频元数据            已重新编码并去除原始 metadata，只保留通用 MP4 编码器标签
交付包内备份文件          app_v1.3.py.bak 已从交付分支删除
```

**关于密钥是否曾被写入并提交：** 当前仓库与可达 Git 提交历史均会在最终打包前扫描。
截至本报告生成时，未发现符合高置信模式的 API Key、Bearer Token、Authorization
Header 值或完整账户信息；代码中出现的变量名和 Header 键名属于正常实现，不是凭据值。

**但有两处只有你能确认**，请自行检查：
1. **你本地的 shell 历史** —— `$env:MINIMAX_API_KEY="..."` 会留在 PowerShell 历史里（`Get-Content (Get-PSReadlineOption).HistorySavePath`）
2. **如果你把 spike 脚本或输出提交到过任何 git 仓库** —— 即使后来删除，历史中仍可恢复

出于稳妥，建议**轮换一次 MiniMax 密钥**。成本几乎为零。

## E. 未验证项

- **真实付费视频生成** —— 适配器只过了 mock 与冷启动。完成了真实视频 API 可行性探测，
  密钥、网络、端点和请求结构验证通过，但生成任务在提交阶段因账户套餐权限被拒绝，
  因此未产生视频，也未接入正式生成链路。**这一项在提交时仍然为空。**
- **真实多模态 API** —— 未配置 Key，重试路径以 mock 证明；真实 429 行为可能与 mock 有细微差异
- **ModelScope 新视频上传版线上实测** —— 本地与全新环境已通过，但本轮新增的视频 UI 和
  两个脱敏样例尚未部署到公开空间；部署后仍需实际跑 Shot 19 / Shot 21 并记录 Trace。
- **并发多用户** —— 单进程共享 CSV，未做并发压测
