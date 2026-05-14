/** @type {import('next').NextConfig} */
const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "https://web-production-d9e54.up.railway.app";

const nextConfig = {
  reactStrictMode: true,
  output: "standalone",
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API_BASE}/api/:path*` },
      { source: "/health/:path*", destination: `${API_BASE}/health/:path*` },
    ];
  },
};

export default nextConfig;
