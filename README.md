# WeChat AI

普通个人微信号 + LLM 自动回复的最小 Windows 项目。

- 微信目标版本：4.1.13.12
- Python：3.10.x 64 位
- 微信自动化：pywechat127 1.9.8（导入名 pyweixin）
- 模型接口：兼容 POST /chat/completions
- 私聊和群聊上下文相互独立，每个会话只保留最近 context_turns 轮
- 没有预览模式：启动后会真实回复
- 推荐用本地网页控制台配置和启停，不要做成需要联网的公网服务

## 目录

    panel.bat              打开本机控制台（推荐）
    app.py                 控制台后端，只监听 127.0.0.1
    bot.py                 自动回复主程序
    config.example.json    好友、群聊配置模板（可上传 GitHub）
    config.json            本机真实配置（Git 已忽略）
    .env.example           LLM 配置模板
    setup.bat              首次安装
    start.bat              命令行后台启动
    stop.bat               安全停止
    status.bat             查看状态
    data/                  PID、锁和日志

## 首次使用

1. 安装并登录微信 4.1.13.12。
2. 安装 64 位 Python 3.10.11，安装时勾选 Python Launcher。
3. 克隆或下载本目录后，双击 setup.bat。
4. 双击 panel.bat，浏览器会打开本机控制台（端口自动选择，避免被 Hyper-V 占用）。
5. 在页面里填写模型接口、密钥、模型名，以及要回复的好友/群。
6. 将配置的好友和群置顶，保持在微信左侧可见会话列表中。
7. 点「测试模型」，通过后再点「保存并启动」。

命令行备用：

    start.bat
    status.bat
    stop.bat

前台运行并直接看报错：

    .venv\Scripts\python.exe bot.py

## 运行条件

- 客户机 Windows 必须保持登录、解锁且不睡眠。
- 微信窗口必须可见、不能最小化，并保持为客户机内部前台窗口。
- 宿主机可以正常使用；不要通过会断开桌面的 RDP 会话运行。
- 微信输入框不能留有人工草稿。
- 建议客户机显示缩放设为 100%。
- 控制台只给本机用，不要映射到公网。

微信失去前台时程序会暂停并重新建立消息快照，但不会清空当前进程中的 LLM 上下文。停止程序、重启程序或重启虚拟机后，内存上下文会清空。

## 群聊触发

config.json 的 group_mode 支持：

- mention：只有 @bot_names 中的昵称才回复。
- prefix：只有以 group_prefix 开头才回复。
- all：群内所有新文本都回复，不推荐用于大群。

## 风险

这是非官方微信 UI 自动化方案。微信升级、界面变化、缩放变化、锁屏或失去桌面渲染都可能导致失效。请只允许明确的测试好友和测试群，并先做小范围验收。
