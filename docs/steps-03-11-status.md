# 步骤 3–11 实施状态

更新日期：2026-09-12。按最新要求，先完成代码，不逐步要求真实微信验收。下列提交和标签是**实现里程碑，不是验收通过证明**。

开发分支：`codex/features-3-11`；远程：`WeChat-AI`。暂不合并到 `test/main`，统一验收后再决定合并。基线标签：`steps-03-11-baseline`。

| 步骤 | 已接入能力 | 阶段提交 | annotated tag |
|---:|---|---|---|
| 3 | owner/admin/user/blocked、聊天权限、审计与低风险命令 | 64f1ce7 | step-03-admin-permissions |
| 4 | 角色、优先级绑定、修订和回滚 | 397f2fb；修正 ea46cd2 | step-04-custom-roles |
| 5 | 可见会话发现、审批、用户备注、旧名单单次导入 | d9a1ee6 | step-05-private-users |
| 6 | 群昵称解析、别名、歧义/改名 pending、诊断 | 1755f5a | step-06-group-members |
| 7 | private/shared/member/hybrid、有限历史与范围清空 | 473e33f | step-07-member-contexts |
| 8 | scrypt、owner 初始化、登录/CSRF、后台管理 API | 1e358f6 | step-08-web-admin |
| 9 | 持久队列、重试、租约、重启恢复、unknown 人工处理 | c5cfc2f；清空队列保护 415ab57 | step-09-durable-queue |
| 10 | 图片/音频/PDF/TXT/MD/DOCX、保留期与能力检查 | 3ba52c2；环境配置 ab2a12e | step-10-inbound-media |
| 11 | 版本化知识库、FTS5/embeddings/RRF、授权只读工具 | ff67578 | step-11-knowledge-tools |

后续收尾提交修复后台权限请求缺失 scope、补齐角色知识模式选择、修复 UTF-8 中文文档，并明确旧配置名单不再是管理入口。新安装示例不预先批准任何聊天；不会覆盖已有运行配置。

## 当前验证记录

- `415ab57`：本地 132 项离线检查通过；ruff、编译和 Git 运行时文件检查通过。
- 对应 Windows CI：run `34674138884`，结果 success。
- 后续提交的 CI 以 GitHub Actions 上**匹配提交 SHA**的记录为准，不沿用旧 SHA 的通过状态。
- 未启动实际 Bot，未连接真实微信、发送真实消息或请求用户配置的 LLM。
- 数据库源码已到 v10；实际本机数据库由下一次正常启动自动备份并迁移，不为检查而强制迁移或删除真实数据。
- 各步骤标签已推送；已发布标签不移动。收尾修复位于功能分支后续提交和最终实现交付标签中。

## 必须在最后统一验收的边界

1. 微信 4.1.13.12 的 UIA 昵称、方向、媒体行、复制菜单、音频缓存和原生转文字是否可可靠读取。
2. 两个群成员交替对话和媒体触发是否不串台；同群重名/改名是否停止自动处理。
3. 实际模型服务的 vision、native tools、embeddings、transcription 是否分别可用；未通过的能力保持禁用。
4. 失焦、重启、网络错误、草稿冲突和 unknown 恢复是否不重复发送。
5. 一个好友和一个测试群的 8 小时、24 小时连续试运行及最终发布判断。

统一清单见 `acceptance-steps-03-11.md`。旧 `acceptance-step-03.md` 仅保留历史，不再要求执行其逐步门禁或重新打标签。
