import type { Metadata } from "next";
import "./globals.css";
import "./workspace-v2.css";
import "./brand.css";

export const metadata: Metadata = {
  title: "OpenBricks · An open home for your data work",
  description: "An open workspace for Spark notebooks, scheduled jobs, SQL Server, object storage, and remote compute.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="en" suppressHydrationWarning><head><script dangerouslySetInnerHTML={{ __html: "try{var t=localStorage.getItem('openbricks-theme');document.documentElement.dataset.theme=(t==='light'||t==='dark')?t:(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light')}catch(e){}" }} /></head><body>{children}</body></html>;
}
