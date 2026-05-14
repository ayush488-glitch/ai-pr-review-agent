import "./globals.css";
import type { Metadata } from "next";
import { SWRProvider } from "@/lib/swr";
import { Shell } from "@/components/Shell";

export const metadata: Metadata = {
  title: "AI PR Review",
  description: "Dashboard for the AI Pull Request Review Agent",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body>
        <SWRProvider>
          <Shell>{children}</Shell>
        </SWRProvider>
      </body>
    </html>
  );
}
