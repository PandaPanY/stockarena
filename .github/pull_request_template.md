## 改了什么

<!-- 一两句话说明改动和原因；关联的 Issue 写成 Closes #编号 -->

## 怎么验证的

<!-- 新增或修改了哪些测试；界面改动请附截图 -->

## 检查清单

- [ ] 后端 `uv run ruff check .`、`uv run pytest -q` 通过
- [ ] 前端 `npm run lint`、`npm run format:check`、`npm run type-check`、`npm test` 通过
- [ ] 数据库结构有变化时写了迁移，已有安装升级后的行为不变
- [ ] 用户能看到的行为有变化时，更新了 `docs/USER_GUIDE.md` 或 `docs/FEATURES.md`
- [ ] 没有提交密钥、内网地址、个人路径
- [ ] 涉及策略评估时，遵守了 CONTRIBUTING.md 里「研究方法的约定」
