# QQ 群本地钓鱼小游戏

基于 FastAPI + OneBot 11 WebSocket 的纯本地 QQ 群钓鱼机器人，不调用 DeepSeek 或任何 AI API。

## 启动

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 4677
```

NapCat/LLOneBot 反向 WebSocket：`ws://127.0.0.1:4677/ws`（同时支持 `/onebot/v11/ws`）。数据默认保存在 `data/bot.sqlite3`，可用 `DB_PATH` 覆盖。

## 游戏规则

- 注册初始 100 分；每次钓鱼消耗 5 分。
- 普通成员每日最多 3 次，间隔至少 5 分钟；群主豁免次数和冷却，但仍消耗积分。
- 鱼种、空竿和 0.5/2/10 倍积分奖励均按 `QQ_AIreadme.md` 的固定权重本地随机。
- 排行榜只统计鱼塘内鱼种价值，不统计直接积分奖励。
- 所有数据按“群号 + QQ号”隔离。

## 指令

- `@机器人 注册`
- `@机器人 钓鱼`
- `@机器人 查看我的鱼塘`
- `@机器人 卖鱼`，随后输入 `小鲫鱼×2 彩虹锦鲤×1`、`一键卖出`，最后发送 `确认`
- `@机器人 鱼塘排行`
- 群主：`@机器人 @用户 充值XX积分`、`扣减XX积分`、`清零积分`

卖鱼订单 5 分钟超时；确认前发送其他内容会取消订单，确认时会再次校验库存并原子结算。

## 退群清理

收到 OneBot `group_decrease` 通知时：普通成员离开/被踢会删除该成员在该群的账号、积分、鱼塘库存和未完成订单；机器人自身离开群聊会删除整个群的所有钓鱼数据。

## 健康检查

访问 `/health` 返回本地小游戏模式和服务状态。
