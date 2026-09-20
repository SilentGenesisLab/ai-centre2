"use client";

import { FormEvent, useState } from "react";

const BASE_PATH = "/admin";

export function LoginForm() {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const response = await fetch(`${BASE_PATH}/api/auth/login`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail || "登录失败");
      }
      window.location.assign(`${BASE_PATH}/overview`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="login-card" aria-labelledby="login-title">
      <div>
        <p className="kicker">ADMIN CONSOLE</p>
        <h2 id="login-title">登录管理后台</h2>
        <p className="subtle">会话将在 8 小时后自动失效。</p>
      </div>
      <form onSubmit={submit}>
        <label>
          管理员账号
          <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required />
        </label>
        <label>
          密码
          <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required autoFocus />
        </label>
        {error && <div className="form-error" role="alert">{error}</div>}
        <button className="primary-button" type="submit" disabled={busy}>
          {busy ? "正在验证…" : "进入控制台"}
        </button>
      </form>
      <p className="login-footnote">生产密钥只保存在服务器，不会发送到浏览器。</p>
    </section>
  );
}
