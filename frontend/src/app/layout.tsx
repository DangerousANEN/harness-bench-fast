import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Harness Bench Fast ◆ Panel",
  description: "Web management panel for harness-bench-fast",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
