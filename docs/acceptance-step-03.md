# 第 3 步：限定好友/群真实验收（尚未执行）

默认只在已确认的一个测试好友和一个测试群进行；需用另一微信账号发消息。不得发送给其他联系人，不得用历史聊天内容冒充本次验收。记录结果时只保留提交 SHA、项目/结果/时间，不提交真实昵称、聊天正文、截图或密钥。

## 准备

1. 记录当前 git rev-parse HEAD，并确认该 SHA 的 Windows offline tests 已通过。
2. 双击 panel.bat。确认数据库显示 v2；本机 data/backups/ 有 pre-v2 备份。
3. 回复对象只保留这一个测试好友和测试群，置顶并可见，填写本账号准确的 bot_names。
4. 微信 4.1.13.12 在虚拟机内登录、解锁、前台可见；输入框无草稿。
5. 点击「保存并启动」是**真实发送**，不是预览。切回微信，等 ready history_rebaselined=true 后再从另一账号发新消息。

## 验收项目

- [ ] 私聊 user：发普通问题得到 AI 回复；/ai help、/ai status 返回固定文字，无新增 LLM 请求。
- [ ] 私聊 admin：后台「身份与权限」选中该好友，当前聊天 → admin；切回微信并发新的 /ai status，显示 admin。
- [ ] 私聊 blocked：将该范围改为 blocked；发普通问题、/ai help、/ai status、/ai reset，均不回复且不调用模型。
- [ ] 解封：在正确范围改回 user 或移除 blocked；检查无其他全局 blocked；新消息恢复，blocked 期间消息不补回。
- [ ] 自己出站：使用机器人本账号手动发送 /ai reset、/ai status，不触发命令/回复/清空操作。
- [ ] 私聊 reset：先积累至少两轮，然后好友发送 /ai reset；只清空该私聊，群历史不变；审计有 context.clear 与 command.reset。
- [ ] 群 pending：一个尚未登记的测试成员发送普通触发文本和 /ai status；不回复。刷新后台身份列表，显示 pending。身份不明应跳过，不猜测。
- [ ] 群批准 user：人工核对头像/UI 昵称，确认群内没有重名，再批准。切回微信并等待重新建立基线；不补回批准前消息。普通有效触发文本可回复，/ai status 不回复。
- [ ] 群 admin：在「当前聊天」授予该成员 admin；新的 /ai status 可回复。另一未批准/普通成员不能执行该命令。
- [ ] 群 blocked：已批准成员改为 blocked 后不回复、不调用 LLM；群其他已批准成员仍按其权限工作。
- [ ] 群 reset 边界：本步群仍是共享历史，/ai reset 只提示尚不支持成员清空，不能删除整群历史。
- [ ] 重启：停 Bot、重开 Panel/Bot，权限与审计仍存在，已完成上下文仍存在，不重复回复历史。
- [ ] 失焦：回复前切到其他窗口时机器人暂停，不覆盖草稿；恢复前台后不回放旧消息。
- [ ] 人工草稿：聊天输入框留草稿时停止发送；任何 unknown 日志出现都应停止、人工检查，不能尝试自动重发。
- [ ] 审计：批准/停用/拉黑/解封/权限变化/清空有记录，记录中无 API Key 和命令正文。

## 通过条件与下一步

全部适用项通过，且没有 identity/permission 漏检、重复发送、草稿覆盖、SQLite 异常，才能确认本步。若 UIA 无法可靠确定入站方向或成员昵称，视为未通过，先提交修复，不绕过识别安全检查。

确认当前 SHA 的 CI 与上述验收均通过后才运行：

~~~powershell
git tag -a step-03-admin-permissions -m "Step 03: offline CI and limited friend/group acceptance passed"
git push WeChat-AI step-03-admin-permissions
git status --short
~~~

未真实验收时，不运行上面两条标签命令，不进入步骤 4。
