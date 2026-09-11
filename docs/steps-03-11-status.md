# 步骤 3–11 实施状态与门禁

基线：16c5c4a（test）；功能分支：codex/features-3-11；远程：WeChat-AI。

本文件记录实现边界，不代替 GitHub CI 或人工微信验收。**未完成真实验收的步骤不能打验收标签，也不能进入下一步。**

| 步骤 | 数据库 | 当前状态 | 计划验收标签 |
|---|---:|---|---|
| 3 权限与审计 | v2 | 功能及离线测试已实现；真实微信验收待完成；CI 以对应提交为准 | step-03-admin-permissions |
| 4 角色 | v3 | 未开始，等待步骤 3 门禁 | step-04-custom-roles |
| 5 私聊发现/管理 | v4 | 未开始 | step-05-private-users |
| 6 群成员身份 | v5 | 未开始 | step-06-group-members |
| 7 成员上下文 | v6 | 未开始 | step-07-member-contexts |
| 8 后台登录/升级 | v7 | 未开始 | step-08-web-admin |
| 9 持久队列 | v8 | 未开始 | step-09-durable-queue |
| 10 入站媒体 | v9 | 未开始 | step-10-inbound-media |
| 11 知识库/工具 | v10 | 未开始 | step-11-knowledge-tools |

## 本步已实现

- 顺序迁移 v1 → v2；SQLite backup API 迁移前备份，跨进程迁移锁，事务、完整性/外键校验，保留最近 10 份自动备份。
- principals / access_grants / audit_events；私聊和群成员身份绑定已登记聊天。
- 权限：显式 blocked 优先于一切授权；聊天授权优先于全局授权；默认 active 用户为 user。
- 只有 Web 账户可被授予 owner，数据库触发器和服务层同时限制。管理员不能管理 owner、管理员或其范围外的聊天。
- 本步 Web 尚无登录账户：仅 127.0.0.1 为临时 owner，验证 Host/Origin、拒绝代理头，所有修改接口（含旧接口）校验 CSRF；这不是第 8 步的账户登录系统。
- 配置白名单仍是目标来源；配置内私聊初始化 active；识别到的群昵称先 pending，人工批准后才处理后续新消息。
- 权限检查在模型调用前；发送前再次检查，避免请求期间被拉黑后仍发送。
- /ai help、/ai status、/ai reset 不调用模型，不污染模型上下文。群内 status 只允许已批准 admin；命令需要 UIA 证明为入站方向。
- 不能以 @机器人 或 /ai 前缀推断发言者身份；自己的消息、身份不明和 pending 身份不能执行命令。
- 私聊 reset 只清空该私聊并原子记录审计。**本步仍是群共享上下文，群 reset 暂拒绝删除历史，等步骤 7 的成员 scope 到位后开放。**
- 输入框被写入后的任何异常均按 unknown 处理并停止，不把部分成功误记为安全失败。
- Windows/Python 3.10 CI；合成控件树、fake adapter、临时数据库、无网络自动化测试。业务模块覆盖率门禁 80%。

## 尚未实现，不能误认为已经具备

- 昵称不是稳定微信 ID。本步没有群名册同步、同群同名自动检测、改名合并；授权前必须人工确认群内无重名，发现疑点应停用。步骤 6 才实现完整冲突诊断。
- 未实现陌生私聊自动发现/审批（步骤 5）；未实现成员独立上下文（步骤 7）。
- 未实现后台密码、恢复码、登录会话（步骤 8），不能把当前页面开放到公网或映射到宿主机公网地址。
- 未实现持久任务队列、模型自动重试、出站气泡双重核验（步骤 9）。
- 未实现媒体理解、知识库和工具（步骤 10–11）。
- 未执行真实好友/群的权限验收，未执行 8 小时/24 小时试运行。

## 开发检查

在项目根目录执行：

~~~powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall -q -x '[\\/](?:\.venv|\.git|data|\.cache)[\\/]' .
.\.venv\Scripts\python.exe -m pytest -m "not live" --cov --cov-report=term-missing
git diff --check
.\.venv\Scripts\python.exe tools/check_tracked_files.py
~~~

覆盖率统计范围明确为 storage、migrations、permissions、commands、audit；另有 Bot 编排、Flask API、合成 UIA 和发送异常测试。这个百分比不是整个微信 UI 自动化代码的覆盖率。默认测试不访问真实网络，不连接微信；live 标记必须显式 --run-live，CI 不启用它。真实验收按 [步骤 3 清单](acceptance-step-03.md) 人工执行，不能拿 fake 测试结果充当真实结果。

## 每步 Git 门禁

每步只提交相应代码、合成测试、依赖与文档；先检查暂存区，不能提交 .env、config.json、data/（仅 .gitkeep 除外）、数据库、日志、截图、附件、知识库或恢复码。

1. commit + push 功能分支，CI 检查对应 SHA，不能沿用上一个提交的结果。
2. 完成该步限定好友/群真实验收；需要修复则新增 fix/test 提交并重跑 CI。
3. 两者都通过后才创建该步 annotated tag 并推送；禁止改写已发布标签。
4. git status --short 为空后进入下一步。
5. 所有步骤完成才能创建 steps-03-11-complete、合并 test；再完成 8h + 24h 限定试运行，最后合并 main。

## 数据库回滚注意

备份名 pre-vN-时间.db 表示**升级到 vN 之前**的数据库，内部版本通常是 N-1，不能凭标签名称猜测。

- 本步 v2 回退到基线 v1，应使用 pre-v2-*.db。
- 将来 v3 回退到本步 v2，应使用 pre-v3-*.db，而不是 pre-v2。
- 停止 Bot 和 Panel，用 backup API 备份当前库，检查备份内部 PRAGMA user_version 和 integrity_check，再恢复匹配版本的代码/数据库。
- 正式分支通过 git revert 回滚，不强推；数据库无向下迁移。
- 恢复前妥善处理旧的 WAL/SHM，不能在进程仍打开数据库时用文件覆盖，也不能把新数据库的 WAL 留给恢复的旧库。
- data/backups/ 含聊天记录，不提交 Git，不上传公共工单；代码回滚不代表数据库已回滚。
