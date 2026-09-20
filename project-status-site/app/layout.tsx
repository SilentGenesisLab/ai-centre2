import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "AI Centre 2 管理后台",
    template: "%s · AI Centre 2",
  },
  description: "AI Centre 2 生产任务、GPU、模型、质量与项目状态管理后台。",
  icons: {
    icon: [{ url: "/admin/favicon.svg?v=3", type: "image/svg+xml" }],
    shortcut: "/admin/favicon.svg?v=3",
  },
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning data-brand="ai-centre-2">
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var m=localStorage.getItem('ai-centre-theme')||'system';var d=m==='dark'||(m==='system'&&matchMedia('(prefers-color-scheme: dark)').matches);var r=document.documentElement;r.dataset.theme=d?'dark':'light';r.dataset.themeMode=m;r.style.colorScheme=d?'dark':'light'}catch(e){}})();`,
          }}
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
