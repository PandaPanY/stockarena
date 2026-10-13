"""访问保护：单用户密码登录。

- 密码用 scrypt 加盐哈希保存在 users.password_hash，首次打开页面时由用户自己设置；
- 登录后签发 HttpOnly Cookie（HMAC 签名，30 天有效），密钥由 data/secret.key 派生；
- 修改密码或「退出所有设备」时 users.session_gen 加 1，之前签发的凭证全部失效；
- AuthMiddleware 拦截 /api、/ws 与接口文档，只放行登录相关接口；
- 忘记密码时在服务器上执行 `uv run python -m app.auth reset-password` 清除密码，再到页面上重新设置。
"""
