# 部署指南

StockArena 是一个自部署的 Web 应用：后端（FastAPI）同时提供前端页面，所有数据保存在一个数据目录里。
你可以用 Docker 一键启动（推荐），也可以直接在 Linux / macOS / Windows 上运行。

> 使用前请阅读 [免责声明](../DISCLAIMER.md)。各配置项的详细说明见 [CONFIG.md](CONFIG.md)。

## 1. 准备

| 项目 | 建议 |
|---|---|
| 机器 | 一台常开的电脑或服务器（收盘后、晚上有定时任务）；2 核以上 |
| 内存 | 4 GB 以上（回测时行情矩阵常驻内存，每份约 0.2～0.4 GB，最多缓存 8 份） |
| 磁盘 | 5 GB 以上可用空间（数据库与行情缓存会随时间增长） |
| 网络 | 能访问国内行情网站（新浪、腾讯、东方财富、巨潮、中证指数、Baostock 等）；境外 AI 模型（OpenAI、Claude、Gemini 等）可能需要代理 |
| AI 模型（可选） | 云端 API（DeepSeek、通义、Kimi、OpenAI、Claude、Gemini 等）或本地模型（Ollama、LM Studio、llama.cpp、vLLM）。不配模型也能使用回测、组合和模拟 |

## 2. Docker 部署（推荐）

需要 Docker 24 以上（自带 `docker compose`）。

```bash
git clone https://github.com/PandaPanY/stockarena.git stockarena
cd stockarena
docker compose up -d --build
```

第一次构建要下载基础镜像和依赖，需要几分钟。启动后：

1. **查看首次设置码**（防止别人抢先设置管理员密码）：

   ```bash
   docker compose logs | grep 设置码
   ```

   也可以直接看数据目录里的 `data/setup-code.txt`。

2. 浏览器打开 `http://服务器地址:18080`，填写设置码，设置管理员（用户名 `admin`）的密码。
3. 进入「策略实验室」准备数据（只有管理员能操作），两种方式：
   - 「**快速开始**」：先下载上证50 + 核心 ETF（约 60 个标的），实测约 4 分钟就能试算第一个策略，完整数据随后自动在后台准备；
   - 「**准备完整数据**」：直接下载沪深 300 全时点股票池的历史成分、行情、财报和行业分类，第一次 20～30 分钟。

   之后每个交易日晚上自动增量更新。完整数据准备好后，才能一键创建风格策略库。
4. 需要多人使用时，在右上角账号菜单 →「用户管理」里打开「**开放注册**」（新安装默认关闭）。
5. 在「配置中心 → AI 模型」添加模型（可选），即可让 AI 优化策略。

常用命令：

```bash
docker compose logs -f              # 查看日志
docker compose restart              # 重启
docker compose down                 # 停止（数据保留在 ./data）
```

数据目录 `./data` 挂载在容器的 `/app/data`，删除容器不会丢数据。要修改配置时，复制 `config/config.example.yaml` 为 `config/config.yaml`，
在 `docker-compose.yml` 里取消对应挂载行的注释，再 `docker compose up -d`。常用设置也可以直接写在 `docker-compose.yml` 的 `environment` 里（`SA_` 开头的环境变量，见 [CONFIG.md](CONFIG.md)）。

> 技能（Skills）功能需要在容器里再运行 Docker，Docker 部署方式下暂不支持，默认关闭；需要时请用第 3 节的方式部署，见 [SKILLS.md](SKILLS.md)。

## 3. 直接运行（不用 Docker）

需要 Python 3.13+、[uv](https://docs.astral.sh/uv/) 0.9+、Node.js 24+。

```bash
git clone https://github.com/PandaPanY/stockarena.git stockarena
cd stockarena

# 打包前端
cd frontend && npm ci && npm run build && cd ..

# 配置：让后端托管打包好的前端页面
cp config/config.example.yaml config/config.yaml
# 编辑 config/config.yaml，加一行：frontend_dist: ../frontend/dist（相对路径以 backend/ 为准，也可以写绝对路径）

# 安装后端依赖并启动
cd backend
uv sync --no-dev
uv run uvicorn app.main:app --host 0.0.0.0 --port 18080
```

首次设置码打印在启动日志里（也在 `data/setup-code.txt`），之后的步骤同第 2 节。

> 监听地址和端口由启动命令的 `--host`、`--port` 决定。`0.0.0.0` 表示局域网内其他设备可以访问；只在本机使用时改为 `127.0.0.1`。

### 3.1 用 systemd 常驻运行（Linux）

`scripts/stockarena.service` 是用户级 systemd 服务模板，假设程序放在 `~/stockarena`：

```bash
mkdir -p ~/.config/systemd/user
cp scripts/stockarena.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now stockarena
loginctl enable-linger $USER        # 退出登录、重启后服务继续运行
journalctl --user -u stockarena -f  # 查看日志
```

### 3.2 从开发机一键更新服务器

在开发机的仓库根目录执行 `bash scripts/deploy.sh`：检查工作区已提交 → 打包前端 → 上传到服务器的临时目录并检查完整 → 停止服务并备份数据库（保留最近 10 份）→ 整体替换代码 → 同步依赖 → 启动并检查。

替换是整目录替换，服务器上的代码与仓库完全一致，仓库里删掉的文件不会残留。保留不动的只有 `data/`、`config/config.yaml` 和 `backend/.venv`。上一版保存在服务器的 `.previous` 目录，出问题时执行 `bash scripts/deploy.sh --rollback` 换回去（再执行一次可以换回新版）。如果新版本带了数据库迁移，回滚代码后旧版本可能无法启动，这时还要从 `data/backup/` 恢复部署前的数据库。

服务器地址用环境变量 `SA_DEPLOY_HOST` 指定，或写在不进版本库的 `scripts/deploy.env` 里（如 `SA_DEPLOY_HOST=user@10.0.0.2`）；需要能免密 ssh 登录。

## 4. HTTPS 与反向代理

在公网或不信任的网络中使用时，请放在反向代理后面并启用 HTTPS，同时只让反向代理访问 18080 端口（Docker 方式把端口映射改为 `127.0.0.1:18080:18080`）。

Caddy（自动申请证书）：

```
stock.example.com {
    reverse_proxy 127.0.0.1:18080
}
```

Nginx：

```nginx
server {
    listen 443 ssl;
    server_name stock.example.com;
    # ssl_certificate / ssl_certificate_key ...
    client_max_body_size 20m;
    location / {
        proxy_pass http://127.0.0.1:18080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }
}
```

设置 `SA_PUBLIC_URL=https://stock.example.com` 后，推送消息里会附上页面链接。

> 登录限速和注册限速按来源 IP 计算。uvicorn 默认只信任 `127.0.0.1` 发来的 `X-Forwarded-For`：直接运行、反向代理在同一台机器上时不用改；
> Docker 方式下代理的请求来自容器网络的网关地址，不是 `127.0.0.1`，所有人会共用一个限速额度。端口已经按上面改成只映射到 `127.0.0.1`
> （外面只能经过反向代理访问）时，可以在 `docker-compose.yml` 里加
> `command: ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18080", "--forwarded-allow-ips=*"]`。
> 端口直接对外开放时不要这样设置，否则别人可以伪造来源 IP 绕过限速。

## 5. 网络与代理

- 国内行情网站经代理访问容易被断开，程序启动时会把这些域名加入 `NO_PROXY`，自动直连（配置项 `direct_hosts`）。
- 访问境外 AI 模型需要代理时，设置环境变量 `HTTP_PROXY` / `HTTPS_PROXY`。本机和局域网地址（如本地模型）会自动绕过代理。

## 6. 备份与恢复

所有数据都在数据目录（默认 `data/`，Docker 方式为 `./data`）：

| 文件 | 内容 |
|---|---|
| `stockarena.db` | 数据库：用户、策略、组合、模拟账户、设置等 |
| `secret.key` | **主密钥**：加密保存的 AI 模型 API Key、推送密钥都靠它解密 |
| `cache/` | 行情缓存（丢了可以重新下载） |
| `logs/` | 日志 |

**`stockarena.db` 和 `secret.key` 必须一起备份**。只恢复数据库、主密钥不对时，已保存的 API Key 和推送密钥都要重新填写（页面上会提示「无法解密」）。

最简单的备份方式是停止服务后复制整个数据目录：

```bash
docker compose stop && cp -a data data-backup-$(date +%Y%m%d) && docker compose start
```

数据库使用 SQLite 的 WAL 模式，服务运行中直接复制 `stockarena.db` 可能得到不完整的文件；不想停服务时，用 SQLite 的在线备份：

```bash
python3 -c "import sqlite3; sqlite3.connect('data/stockarena.db').backup(sqlite3.connect('stockarena-backup.db'))"
```

恢复：停止服务，把备份的 `stockarena.db`、`secret.key` 放回数据目录，再启动。

## 7. 升级

```bash
git pull
docker compose up -d --build        # Docker 方式
```

不用 Docker 时：`git pull` 后重新打包前端、`uv sync --no-dev`，再重启服务。数据库迁移在启动时自动执行（`auto_migrate`）。**升级前请先备份**（第 6 节）。

## 8. 忘记管理员密码

清除管理员密码，并生成新的首次设置码（打印在屏幕上），然后打开页面重新设置：

```bash
# Docker 方式
docker compose exec -u stockarena stockarena python -m app.auth reset-password
# 直接运行
cd backend && uv run python -m app.auth reset-password
```

普通用户忘记密码时，由管理员在「用户管理」里重设。

## 9. 常见问题

| 问题 | 处理 |
|---|---|
| 页面要求填写「首次设置码」，找不到 | 在启动日志里搜「设置码」，或打开数据目录下的 `setup-code.txt` |
| 「准备数据」失败或很慢 | 多半是数据源网络问题：确认服务器能访问新浪、腾讯、Baostock 等网站，没有被代理拦截；失败后可以再点一次，已下载的部分会保留 |
| AI 模型显示「无法解密，请重新填写」 | 主密钥 `secret.key` 被删除或替换过。恢复原来的 `secret.key`，或在「配置中心」重新填写 API Key |
| 启动时报「主密钥文件格式不对」 | `secret.key` 被改坏了。有备份就恢复；没有备份时删除它，重启后生成新密钥，再重新填写各个 API Key 和推送密钥 |
| 端口被占用 | 换一个端口：Docker 方式改 `docker-compose.yml` 的端口映射（如 `"18090:18080"`）；直接运行时改 `--port` |
