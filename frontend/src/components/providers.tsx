"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

/**
 * 全局的服务端状态容器。
 *
 * QueryClient 用 useState 建而不是模块级单例：模块级单例在服务端渲染时会被
 * 所有请求共用，一个用户的数据会漏给另一个用户。
 *
 * 实时性策略用轮询不用 WebSocket（§16.1）：实现成本接近 0、没有连接管理，
 * 而"看进度"这个需求本来就不需要毫秒级。
 */
export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // 评测跑十几分钟，数据不会秒级变化，重复请求没意义
            staleTime: 5_000,
            retry: 1,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}
