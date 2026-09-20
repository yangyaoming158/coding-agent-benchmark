import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  experimental: {
    // Next 16.3 的 CLI 子进程在当前 Node 环境拿不到 `tsc --showConfig` 的 stdout。
    // 改用同版本 TypeScript 的 compiler API，生产构建仍会执行完整类型检查。
    useTypeScriptCli: false,
  },
};

export default nextConfig;
