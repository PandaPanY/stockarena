# 配置参考

## 配置从哪里来

优先级从高到低：

1. 环境变量（前缀 `SA_`，如 `SA_PUBLIC_URL=http://10.0.0.2:18080`）
2. 仓库根目录的 `.env` 文件（样例见 `.env.example`）
3. 配置文件 `config/config.yaml`（样例见 `config/config.example.yaml`；用环境变量 `SA_CONFIG_FILE` 可以指定别的路径）
4. 代码里的默认值

`config.yaml` 和 `.env` 不进版本库，个人部署的设置（地址、路径等）都放在这里。列表类配置用环境变量设置时写成 JSON，如 `SA_DIRECT_HOSTS='["sina.com.cn","qq.com"]'`。

> 交易成本、AI 模型、推送通道、自选股监控、开放注册等设置在页面上修改，保存在数据库里，不在这里配置。

## 配置项

| 配置项 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `data_dir` | `SA_DATA_DIR` | 仓库根目录下的 `data/`（Docker 为 `/app/data`） | 运行时数据目录：数据库、日志、主密钥 `secret.key`、行情缓存、首次设置码。相对路径以启动目录为准，建议写绝对路径 |
| `database_url` | `SA_DATABASE_URL` | 空（使用 `data_dir/stockarena.db`） | 数据库地址。目前只在 SQLite 上测试过 |
| `auto_migrate` | `SA_AUTO_MIGRATE` | `true` | 启动时自动升级数据库结构 |
| `frontend_dist` | `SA_FRONTEND_DIST` | 空（Docker 为 `/app/frontend/dist`） | 由后端托管的前端打包目录，页面和接口用同一个端口。为空时只提供接口（开发时页面由 Vite 提供） |
| `source_url` | `SA_SOURCE_URL` | `https://github.com/PandaPanY/stockarena` | 页面底部「源代码」链接。AGPL-3.0 要求：修改了代码并通过网络向他人提供服务时，需要让这些用户能拿到修改后的源代码，请改成你公开修改后代码的地址 |
| `public_url` | `SA_PUBLIC_URL` | 空 | 对外访问地址，用于推送消息里的页面链接；为空时不附链接 |
| `auth_enabled` | `SA_AUTH_ENABLED` | `true` | 访问保护：接口需要登录。**只在本机自己用、且只监听 `127.0.0.1` 时才可以关闭**；关闭后所有访问者都是管理员 |
| `skills_enabled` | `SA_SKILLS_ENABLED` | 不写 | 技能功能（在 Docker 容器里执行第三方代码）。不写时：已安装过技能的旧安装保持开启，新安装关闭。见 [SKILLS.md](SKILLS.md) |
| `mcp_enabled` | `SA_MCP_ENABLED` | `true` | MCP 服务（`/api/mcp`）：用户用个人 API 令牌从 Claude Code、Cursor 等 AI 客户端查询和研究策略。见 [MCP.md](MCP.md) |
| `auto_run` | `SA_AUTO_RUN` | `true` | 按收盘时间自动执行日终流程、每晚数据更新与 AI 优化、自选股监控等定时任务 |
| `direct_hosts` | `SA_DIRECT_HOSTS` | 国内行情域名列表（见 `backend/app/core/network.py`） | 不经过系统代理、直接访问的域名。启动时追加到 `NO_PROXY` |
| `log_level` | `SA_LOG_LEVEL` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR`。日志同时写到 `data_dir/logs/` |
| `cors_origins` | `SA_CORS_ORIGINS` | `http://127.0.0.1:15173`、`http://localhost:15173` | 允许跨域访问接口的前端地址，只在开发时（Vite 开发服务器）需要 |
| `rule_defaults` | — | 空 | 交易规则的全局默认值（系统默认 → 这里 → `config/rulesets/` 中的规则集，规则集写明的优先）。参数名见 `backend/app/trading/params.py` |
| `env` | `SA_ENV` | `dev` | 运行环境标记，只用于日志和健康检查显示 |

**监听地址和端口**由启动命令决定（`uvicorn ... --host 0.0.0.0 --port 18080`），不读配置文件里的 `host` / `port`。Docker 方式改 `docker-compose.yml` 的端口映射即可。

## 其他环境变量

| 环境变量 | 说明 |
|---|---|
| `SA_CONFIG_FILE` | 配置文件路径，默认 `config/config.yaml` |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | 访问境外 AI 模型需要代理时设置；`direct_hosts` 里的域名、本机和局域网地址会自动直连 |
| `TZ` | 日志显示的时区（Docker 镜像默认 `Asia/Shanghai`）。各市场的定时任务都带明确时区，不受它影响 |
| `SA_DEPLOY_HOST` / `SA_DEPLOY_DIR` / `SA_DEPLOY_PORT` | 只给 `scripts/deploy.sh` 用：服务器地址（必填）、服务器上的目录（默认 `stockarena`）、端口（默认 `18080`）。可以写在不进版本库的 `scripts/deploy.env` 里 |

## 其他配置文件（`config/` 目录，随版本发布）

| 文件 | 内容 |
|---|---|
| `strategies.yaml` | 策略库的初始策略（7 种风格），管理员在策略实验室一键创建 |
| `universes/*.yaml` | 股票池定义（沪深 300、中证 800 及其时点版本等） |
| `rulesets/*.yaml` | 交易规则集（A 股 / 港股 / 美股，现金 / 融资融券） |
| `research/default.yaml` | AI 研究员（优化策略）的提示词 |
| `prompts/`、`reflection/`、`personas.yaml` | AI 对比（竞技场）功能的提示词、复盘模板、投资风格 |

这些文件一般不需要修改；改动后重启生效。
