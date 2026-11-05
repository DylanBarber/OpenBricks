import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  experimental: {
    proxyTimeout: 300_000,
    // External rewrites also clone the request body. Include multipart overhead.
    proxyClientMaxBodySize: `${Number(process.env.OPENBRICKS_MAX_UPLOAD_MB || 100) + 1}mb`,
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${process.env.API_INTERNAL_URL || "http://127.0.0.1:8000"}/api/:path*` }];
  },
  async headers() {
    return [{ source: "/:path*", headers: [
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "X-Frame-Options", value: "DENY" },
      { key: "Referrer-Policy", value: "same-origin" },
    ] }];
  },
};
export default nextConfig;
