# MCP 接入

StockArena 内置一个 [MCP](https://modelcontextprotocol.io)（Model Context Protocol）服务。Claude Code、Cursor 等 AI 客户端连上之后，可以直接在对话里查询你的策略、看优化时间线、试算规则；令牌允许修改时，还能新建策略、提交新版本、启动 AI 优化。

## 怎么连

1. 打开「配置中心 → MCP 接入」，点「新建令牌」。默认只读；勾选「允许修改」才能新建策略、提交版本、启动 AI 优化。
2. 令牌只在创建时显示一次。页面会直接生成下面两种配置，复制即可。

**Claude Code**

```bash
claude mcp add --transport http stockarena http://你的服务器:18080/api/mcp --header "Authorization: Bearer sa_你的令牌"
```

**Cursor 等支持 HTTP 传输的客户端**（`mcp.json`）

```json
{
  "mcpServers": {
    "stockarena": {
      "type": "http",
      "url": "http://你的服务器:18080/api/mcp",
      "headers": { "Authorization": "Bearer sa_你的令牌" }
    }
  }
}
```

只支持本地进程（stdio）的客户端，可以用 [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) 之类的桥接工具，把上面的地址和请求头转成本地进程。

## 能做什么

| 工具 | 权限 | 说明 |
|---|---|---|
| `list_strategies` | 只读 | 自己的策略和公共策略：风格、股票池、冠军版本的开发期指标、版本数、DSR |
| `get_strategy` | 只读 | 说明、检验设置、来源与测试历史、冠军版本（规则文字和 spec）、研究质量、版本列表、研究笔记 |
| `get_version` | 只读 | 某个版本的规则、spec、开发期指标、检验结论 |
| `get_timeline` | 只读 | 优化时间线：每轮诊断、候选与冠军的规则差异、检验 p 值、是否采纳 |
| `get_signal` | 只读 | 冠军版本最新的目标持仓 |
| `get_spec_reference` | 只读 | 策略规格的字段、选项和因子库 |
| `preview_backtest` | 只读 | 试算：只回测开发期、不保存；**每次都会被记录**，下次新建时计入测试次数 |
| `get_run` | 只读 | 一次 AI 优化的进度和日志 |
| `create_strategy` | 修改 | 新建私有策略并完整回测，之前的试算次数一并计入 |
| `submit_version` | 修改 | 给自己的策略提交修改后的规则：回测并做升级检验，计入测试次数 |
| `start_optimization` | 修改 | 用策略设置的研究模型在后台跑几轮 AI 优化（消耗该模型的额度） |

## 规矩和网页一样

- **权限一致**：只能看到自己的策略和公共策略；公共策略能看、能试算，不能改。工具在服务端内部转调网页用的接口，权限检查、数据隔离、交易成本完全相同。
- **不提供留出期结果**：留出期，以及用到留出期的「开发期与留出期排名相关」「滚动选择检验」，还有实时跟踪账户的表现，都不通过 MCP 提供。AI 客户端可以很快地反复尝试，如果它看得到留出期，就会不知不觉按留出期挑策略，留出期就失去了检验意义。这些请在网页上看。
- **试算计入测试次数**：`preview_backtest` 的结果按令牌记在服务器内存里，下次 `create_strategy` 时作为「新建前的试算」一并提交（与网页新建对话框相同），升级门槛和 DSR 都会把它们算进去。服务重启会清空还没提交的记录。
- **改已有策略只能提交版本**：`submit_version` 的每个版本都会保存、计入测试次数，要在开发期显著好于冠军才会升级。

## 安全

- 令牌形如 `sa_` 加 43 位随机字符。数据库只存哈希，令牌原文只在创建时显示一次；每人最多 10 个。
- 令牌**只能访问 `/api/mcp`**：不能登录网页，也不能调用其他接口。反过来，网页的登录凭证也不能访问 MCP 服务。
- 在「MCP 接入」里随时撤销，立即失效；账户被管理员停用后，它的令牌也一并失效。
- 通过 HTTP 访问时，令牌会以明文经过网络。只在可信的局域网里这样用；从公网访问请先配置 HTTPS（见[部署指南](DEPLOY.md)）。
- 管理员可以用配置 `mcp_enabled: false`（环境变量 `SA_MCP_ENABLED=false`）整个关闭 MCP 服务。

## 协议细节

- 传输方式：Streamable HTTP 的无状态子集。只接受 `POST /api/mcp`，每个请求独立，响应直接是 JSON（不用 SSE）；`GET` 返回 405（不提供服务端推送）。
- 支持的协议版本：`2025-06-18`、`2025-03-26`、`2024-11-05`。
- 方法：`initialize`、`ping`、`tools/list`、`tools/call`（`resources/list`、`prompts/list` 返回空列表）。
- 只读令牌的 `tools/list` 只列出只读工具。
- 带 `Origin` 请求头时必须与访问的主机一致，防止 DNS 重绑定。
- 代码在 `backend/app/mcp/`（`server.py` 协议处理，`tools.py` 工具），令牌在 `backend/app/auth/tokens.py`。
