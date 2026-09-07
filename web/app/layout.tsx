import type { Metadata } from "next";

import { ThemeProvider, themeInitScript } from "@/lib/theme";
import "./globals.css";

export const metadata: Metadata = {
  title: "문서 Q&A",
  description: "업로드한 문서에서 근거를 찾아 답하는 봇",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ko" suppressHydrationWarning>
      <head>
        {/* 페인트 전에 저장된 테마를 적용해 첫 프레임의 번쩍임을 막는다. */}
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body>
        <ThemeProvider>{children}</ThemeProvider>
      </body>
    </html>
  );
}
