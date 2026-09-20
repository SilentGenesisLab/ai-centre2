import Link from "next/link";

export default function NotFound() {
  return (
    <main className="empty-page">
      <span>404</span>
      <h1>没有找到这个管理页面</h1>
      <Link className="primary-button" href="/overview">返回工作台</Link>
    </main>
  );
}
