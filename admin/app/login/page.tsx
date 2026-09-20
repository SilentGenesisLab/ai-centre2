import { redirect } from "next/navigation";
import { getAdminSession } from "@/lib/auth";
import { LoginForm } from "@/components/login-form";
import { ThemeToggle } from "@/components/theme-toggle";

export const metadata = { title: "登录" };
export const dynamic = "force-dynamic";

export default async function LoginPage() {
  if (await getAdminSession()) redirect("/overview");
  return (
    <main className="login-shell">
      <div className="login-theme"><ThemeToggle /></div>
      <section className="login-intro">
        <div className="product-mark" aria-hidden="true">AC</div>
        <p className="kicker">AI CENTRE 2 · PRODUCTION</p>
        <h1>把每一次推理，<br />变成可控的生产任务。</h1>
        <p>统一管理唇形驱动、语音、OCR、人脸处理和 GPU 资源。</p>
        <div className="login-signals">
          <span><i className="signal signal-live" />HTTPS 安全入口</span>
          <span><i className="signal signal-live" />服务端密钥代理</span>
          <span><i className="signal" />单管理员模式</span>
        </div>
      </section>
      <LoginForm />
    </main>
  );
}
