# 参与贡献

感谢你愿意改进 StockArena！提交前请花几分钟读完这份说明。

## 提问题

- **数据拉不下来 / 数据不对**：用「数据源问题」模板，写明是哪个数据源、哪只股票或哪个指数、什么日期，附上日志里的报错。免费数据接口经常变化，这类问题最常见。
- **程序错误**：用「问题报告」模板，写明复现步骤、期望结果和实际结果、部署方式（Docker / 直接运行）和版本。
- **新功能**：先开 Issue 讨论，说清楚要解决什么问题，再动手写代码，避免做完了方向不一致。
- **安全漏洞**：不要公开提 Issue，按 [SECURITY.md](SECURITY.md) 私下报告。

## 开发环境

| 工具 | 版本 |
|---|---|
| Python | 3.13+ |
| [uv](https://docs.astral.sh/uv/) | 0.9+ |
| Node.js | 24+ |

```bash
# 后端（终端 1）
cd backend
uv sync
uv run uvicorn app.main:app --reload --port 18080

# 前端（终端 2）
cd frontend
npm install
npm run dev          # 打开 http://127.0.0.1:15173
```

开发时数据保存在仓库根目录的 `data/`（不进版本库）。不要用正在实际使用的数据库调试：后端启动后会按收盘时间执行日终流程、写数据库，两边会分叉。需要真实数据时用数据库副本，并把 `SA_DATA_DIR` 指到别的目录。

## 提交前请检查

```bash
# 后端
cd backend
uv run ruff check .
uv run pytest -q

# 前端
cd frontend
npm run lint
npm run format       # 或 npm run format:check 只检查
npm run type-check
npm test
```

CI 会跑同样的检查，另外还会构建 Docker 镜像并启动检查、用 gitleaks 扫描全部提交历史里的密钥。

## 代码约定

- **注释、文档、提交说明用中文**；代码标识符用英文。
- **金额、价格、费率用 `Decimal`**（`app/core/money.py`），数据库中以字符串存储（`DecimalString` 列类型）。
- **时间以 UTC 存储**（`UTCDateTime` 列类型），只在展示和判断交易时段时转换为市场本地时间。
- **多用户数据隔离**：用户相关的业务表继承 `UserOwnedMixin`（带 `user_id`）；接口用 `current_user_id` 依赖取当前用户，只能读写自己的数据；管理员专属的接口用 `require_admin`。
- **数据库结构变化要写迁移**：`cd backend && uv run alembic revision --autogenerate -m "描述"`，生成后检查内容再提交；已有安装升级后的行为要保持不变（参考 `alembic/versions/` 里已有的数据迁移）。
- **不要提交密钥和个人信息**：API Key、推送密钥、内网地址、个人路径都放在不进版本库的 `config/config.yaml`、`.env` 或页面设置里。
- **换行符**：沿用文件原有的换行符（仓库里同时有 CRLF 和 LF 的文件），`.sh`、`Dockerfile` 必须是 LF（见 `.gitattributes`）。
- 前端格式由 Prettier 统一（`npm run format`），代码检查用 ESLint（`npm run lint`）；后端用 ruff。

## 研究方法的约定（重要）

这个项目的核心是「AI 改策略，但不能骗自己」。涉及策略评估的改动请遵守：

- **留出期只用来检验**：AI 优化、调参、选版本只能看开发期；不要让任何自动流程根据留出期的结果做选择。
- **测试次数要如实记录**：每个被评估过的版本都计入多重检验校正（DSR 等）；另存、导入、复制策略都要继承测试历史，不能「洗掉」。
- **事先写定判定标准**：新的有效性检验，先写好判定标准再看结果（参考 `docs/prereg/` 里的预注册示例），写定后不再修改。
- **避免前视偏差**：任何数据只在它实际可以获得的时点之后使用（如财报按公告日、指数成分按当时的名单）。

## 分享策略

导出的策略文件可以提交到 [`community/strategies/`](community/strategies/README.md)，要求见那里的说明（保留测试历史、只用全时点股票池的结果）。

## 提交 PR

- 一个 PR 只做一件事；改动较大时先开 Issue 讨论。
- 写清楚改了什么、为什么改、怎么验证的；界面改动附截图。
- 新功能和修复都要有测试；改了用户能看到的行为时，同步更新 `docs/USER_GUIDE.md` 或 `docs/FEATURES.md`。
- 提交 PR 即表示你同意以 [AGPL-3.0](LICENSE) 许可证发布你的贡献。
