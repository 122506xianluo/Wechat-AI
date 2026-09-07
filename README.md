# WeChat AI

普通个人微信号 + LLM 自动回复的最小 Windows 项目。

- 微信目标版本：4.1.13.12
- Python：3.10.x 64 位
- 微信自动化：pywechat127 1.9.8（导入名 pyweixin）
- 模型接口：兼容 POST /chat/completions
- 私聊和群聊上下文相互独立，每个会话只保留最近 context_turns 轮
- 没有预览模式：启动后会真实回复

## 目录

    bot.py                 主程序
    config.example.json    好友、群聊配置模板（可上传 GitHub）
    config.json            本机真实配置（Git 已忽略）
    .env.example           LLM 配置模板
    setup.bat              首次安装
    start.bat              后台启动
    stop.bat               安全停止
    status.bat             查看状态
    data/                  PID、锁和日志

## 首次使用

1. 安装并登录微信 4.1.13.12。
2. 安装 64 位 Python 3.10.11，安装时勾选 Python Launcher。
3. 克隆或下载本目录后，双击 setup.bat，或在终端运行：

       setup.bat

4. 编辑 .env：

       LLM_BASE_URL=https://你的接口/v1
       LLM_API_KEY=你的密钥
       LLM_MODEL=你的模型名

5. setup.bat 会从 config.example.json 自动创建 config.json；编辑 config.json。private_chats、groups 和 bot_names 必须与微信界面显示完全一致。
6. 将配置的好友和群置顶，保持在微信左侧可见会话列表中。
7. 启动：

       start.bat

8. 查看状态或停止：

       status.bat
       stop.bat

也可以前台运行并直接看报错：

    .venv\Scripts\python.exe bot.py

## 运行条件

- 客户机 Windows 必须保持登录、解锁且不睡眠。
- 微信窗口必须可见、不能最小化，并保持为客户机内部前台窗口。
- 宿主机可以正常使用；不要通过会断开桌面的 RDP 会话运行。
- 微信输入框不能留有人工草稿。
- 建议客户机显示缩放设为 100%。

微信失去前台时程序会暂停并重新建立消息快照，但不会清空当前进程中的 LLM 上下文。停止程序、重启程序或重启虚拟机后，内存上下文会清空。

## 群聊触发

config.json 的 group_mode 支持：

- mention：只有 @bot_names 中的昵称才回复。
- prefix：只有以 group_prefix 开头才回复。
- all：群内所有新文本都回复，不推荐用于大群。

## 风险

这是非官方微信 UI 自动化方案。微信升级、界面变化、缩放变化、锁屏或失去桌面渲染都可能导致失效。请只允许明确的测试好友和测试群，并先做小范围验收。
