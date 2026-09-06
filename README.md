# QQ AI Bot

独立于 DSH 的 NapCat/LLOneBot + OneBot 11 群聊机器人，使用 DeepSeek API。

## 启动

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY
uvicorn app:app --host 0.0.0.0 --port 4677
```

NapCat/LLOneBot 反向 WebSocket 使用：`ws://127.0.0.1:4677/ws`。机器人通过这条 OneBot WebSocket 连接发送 `send_group_msg` 调用。

机器人仅在被 @ 或命中关键词时调用模型。每群保存已读游标；触发时最多取 `MAX_UNREAD` 条未读消息，成功回复后推进游标。管理员可在群内使用：

- `!关键词 添加 关键词`
- `!关键词 删除 关键词`
- `!关键词 列表`

人设命令（仅群主）：

- `!人设 设置 你是一个活泼但简洁的群聊助手`
- `!人设 查看`
- `!人设 清除`

本地网页管理：启动服务后访问 `http://127.0.0.1:4677/admin`，密码默认为 `.env` 中的 `ADMIN_PASSWORD`（首次未配置时为 `123456ACCA`）。网页可编辑关键词、人设、主动唤醒概率和持续条数；管理页面只接受本机访问。

主动讨论默认每条新消息有 5% 概率唤醒，唤醒后最多回复 10 条后续消息。参数可通过网页或 `.env` 中的 `PROACTIVE_PROBABILITY`、`PROACTIVE_LIMIT` 设置。

关键词和消息状态保存在 `data/bot.sqlite3`。

上下文采用按群共享的滚动摘要：输入估算达到预算的 85% 时自动总结旧消息，默认预算 3000 token，保留最近 10 条消息；摘要最多 1200 字符，压缩后保留 6 条最新消息。可在 `.env` 中调整这些参数。
