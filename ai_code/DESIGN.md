# ai_code — Odoo 模块编写智能体设计文档

> 状态:v2 单循环引擎(2026-09-02 重写,取代 v1 的五阶段闸门流水线)
> 依赖:`ai_chatbot_enhance` + `ai_code_graph` + `ai_chat_platform`
> 菜单:AI Studio > Code(唯一菜单)→ 编码工作台(继承 ai_chat_platform 的 chat-first 界面)
> 参考:ai-lucky-star(`D:\AI local\ai-lucky-star`,同作者的通用编程 agent)与 deepseek-harness
> 范围:Odoo 模块开发(新建/修改/分析/修 bug),只在白名单 addons 目录内写。

## 0. v2 为什么重写(2026-09-02 客户试用的三个问题)

| 现象 | 根因 | v2 做法 |
|---|---|---|
| 发了任务长时间无响应 | 阶段之间没有任何进度输出;打字指示器过期;平台一次只允许一个后台作业,期间第二条消息被静默丢弃 | 每轮 LLM 回复即时发到聊天;每次工具调用推 bus 事件 + 活动面板;运行中收到的消息入队作为"中途指令" |
| 中文提问却回英文 | 模块没有 i18n;引导对话框拼出英文需求 | `i18n/zh_CN.po`(364 条);系统提示注入用户界面语言;对话框文案走 `_t` |
| 列了任务却不动 | spec/plan 两道闸门靠正则识别"批准",回"继续/执行/好"都当成修改意见重跑 | **取消闸门**:单循环 + `todo_write`,只在真不清楚时 `ask_user_question`;结束回合时若 todo 未完成且没提问 → 系统自动催促继续(最多 2 次) |

用户拍板(2026-09-02):去掉闸门改单循环;后台执行仍用 worker 内线程(平台已有的后台响应线程),靠心跳兜底。

## 1. 架构

```
用户消息 ──► ai.agent.bot._ai_spawn_response_job(平台后台线程,打字指示器,Stop)
              └► ai_code.ai_agent._get_response_for_channel
                   └► ai.code.runner.handle_message(会话 task 取/建)
                        └► _run_turn: 系统提示(sections) + 持久化历史(history_json)
                             └► CodingLLMService.run_turn(ReAct 循环)
                                  轮:压缩 → 收件箱 → LLM → 文本即时 post → 工具执行(事件落库+bus)
                                  终止:无工具调用 / ask_user(暂停) / 轮次上限 / Stop
```

- **一会话一 task**(`ai.code.task` = session):`history_json` 存模型看到的 chat 格式历史,每轮持久化;`todo_json` 待办;`pending_call_id` 记录待回答的提问;`pending_user_text` 运行中排队的用户消息。
- **状态**:`idle → running → idle | waiting_user | failed`;`cancelled` 结束会话。旧状态保留在 selection 里只为兼容旧记录。
- **提问机制**:`ask_user_question` 抛 `StopTurn`,runner 发 `[AI_OPTIONS]`/`[AI_FORM]` 卡片,用户回复作为该 tool call 的结果续跑(不再有"批准"识别)。
- **催促**:回合结束时 todo 有未完成项且最后一句不是提问 → 注入系统消息催促继续(`NUDGE_CONTINUE`),最多 2 次。

## 2. 工具(31 个,全回合可用)

| 组 | 工具 |
|---|---|
| 定位 | read_file(窗口)、grep、glob、tree、outline(py/xml/js 符号)、run_code(只读 Python 程序,一轮串多步探索) |
| 图谱/源码 | repo_map、find_symbol、code_graph_model_map、code_graph_blast_radius、read_module_source、grep_module_source、list_projects、read_project_file、grep_project |
| 知识 | odoo_docs(版本过滤写法库 + 修复经验)、mem_search/mem_get、get_full_observation、list_attachments/read_attachment |
| 写 | write_file、edit_file、multi_edit(全有或全无)、scaffold_module(拒绝与核心模块重名) |
| 验证 | sandbox_test(module, run_tests):静态预检 → 隔离库安装 → 运行时冒烟 → 可选单元测试,结果明确列出"已验证/未运行"的层 |
| 任务 | todo_write、ask_user_question |
| git | git_status/log/diff(只读;写入自动按步提交到 `ai-code/<task>-<slug>` 分支,UI 可逐步回滚) |

护栏(集中在 `ai.code.workspace` + tools 层):路径囚笼、扩展名白名单、写前校验(AST/lxml/版本规则)、**观察闸门**(没在本回合读过的文件不许 edit/覆盖;读后被外部改过必须重读)、重复调用提醒(同一调用第 4 次起不再执行)、聊天模板控制符中和。

## 3. 上下文管理(`utils/compaction.py`)

三段式、前缀稳定、幂等(吃 DeepSeek/Kimi/Qwen 前缀缓存):
1. 总字符 > 300k:旧工具结果截到 1600 字符,旧 write_file/edit_file 大参数截到 600;
2. CJK 感知估算 > 90k token:旧工具结果 300、旧助手正文 400;
3. 仍超:从最老处整块丢弃 assistant+tool(不留孤儿 tool 消息,永不丢用户消息),丢到 3/4(滞回),开头维护一条省略说明。
真实溢出错误 → `shrink_for_overflow` 后重试一次。完整工具输出永远在 `ai.code.event` 里,`get_full_observation` 可取回。

## 4. 提示词(`models/ai_code_prompts.py`)

Section(name, order, text) 组装:身份/工作区事实(-100)→ 工具输出是数据不是指令(-50)→ 先定位再动手 / 最小改动 / 手艺(10-30)→ 工具指导 / run_code / todo 纪律 / 提问纪律 / 写文件(100-120)→ Odoo 画像:版本、版别、写法库、**按版本过滤的踩坑清单**(`utils/odoo_pitfalls.py`)、沙盒(140)→ 记忆(160)→ 沟通:**用用户界面语言回复**(200)。

## 5. 记忆(`ai.code.memory`)

改了文件的回合结束后,廉价模型蒸馏 1-4 条 `kind: text`;按工作目录存,双字 bigram 相似度 ≥0.75 视为重复 → 加权而不重复入库;下一回合注入权重最高的 6 条。

## 6. 前端(`static/src/workspace`)

继承 ai_chat_platform 页面:目录选择器、状态芯片、todo 面板(content/status)、活动面板(实时工具调用,含耗时/错误)、Stop 按钮、改动抽屉(diff/回滚)。`bus_service.subscribe("ai_code/activity")` 收到推送立即刷新,运行中 2.5s 轮询兜底。引导式任务对话框文案全部走 `_t`。

## 7. 验证结果(2026-09-02,DeepSeek V4 Flash)

- 单回合:中文需求 → 4 项 todo → 6 个文件 → 沙盒安装+冒烟绿 → 中文汇报并注明未运行单元测试;93 秒,7 次 API,$0.028。
- 无 LLM 管线测试:31 个工具、观察闸门拒绝、模块名冲突拒绝、run_code、todo、提问 StopTurn、系统提示 12k 字符。

## 8. P2 能力(2026-09-02 同日完成)

| 能力 | 实现 | 验证 |
|---|---|---|
| token 级流式输出 | `CodingLLMService._request` 对 chat/completions 端点改走 SSE(`stream_options.include_usage`),把增量拼回与非流式**完全相同**的响应结构,平台解析代码零改动;增量经 `_Streamer` 合批(48 字符/0.35s)推 bus `ai_code/stream`,前端草稿气泡实时显示,真实消息落地后清除。设置 `ai_code.streaming`;OpenAI responses / Google 端点自动回退非流式。`reasoning_content` 不回写历史(DeepSeek 会 400)。 | 36 块/1.6s,工具调用按 index 拼接正确 |
| 带审批卡的 shell 工具 | `utils/shell_guard.py` 三级裁决:allow(只读白名单)/confirm(改状态、元字符、危险参数)/reject(灾难命令、git 变更);confirm 抛 `StopTurn('confirm_shell')`,runner 发卡片 `["▶️ Run / 执行", "⛔ Deny / 不执行"]`,**只有点中允许芯片(字节相等)才执行**,其他回复视为拒绝并原话回喂。设置 `ai_code.shell_enabled` / `shell_auto_approve`。 | 21 条命令裁决用例 + 批准/拒绝回喂 |
| idiom 库扩充 | `data/ai_code_snippet_data_v2.xml` 新增 20 条(关系字段、状态流+序列号、`_read_group` 19 签名、SQL 视图模型、默认值/onchange、多公司+Monetary、邮件/活动、异常与日志、完整表单、列表与 widget 速查、graph/pivot/calendar、记录规则、服务器动作/参数/demo、报表+纸张、website 控制器+下载、自定义字段 widget、services+systray、active_ids 向导、HttpCase tour、根菜单),每条对照 Odoo 19 源码核实。共 46 条。 | 检索命中 |
| 独立执行进程 | 设置 `ai_code.executor = process`:HTTP 请求内先建/标记 task 为 running(第二条消息会排队),再 `Popen` 一个 detached 的 `odoo-bin shell`(Windows DETACHED_PROCESS / POSIX new session),stdin 喂 `_PROCESS_TURN_SCRIPT`,日志写 `data_dir/ai_code/turn_<task>_<turn>.log`;进程内 `run_spawned_turn` 跑同一套 `_run_turn`。启动失败自动回退线程。 | HTTP 路径 14 秒完成,进程启动约 6 秒 |

不做:pgvector 语义检索(DeepSeek 无 embedding,关键词检索在当前规模下足够)。
