/**
 * 后端 API 的访问入口。
 *
 * 类型不手写：跑 `npm run gen:api` 从后端的 OpenAPI 生成到 `src/lib/api-types.ts`。
 * 手写类型一定会和后端漂移，而且漂移了不会报错，只会在运行时拿到 undefined。
 */

import type { components } from "./api-types";

/** 后端地址。开发时后端跑在 8000，前端跑在 3000，跨域由后端放行。 */
export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** 发一个 GET 请求。失败时抛 ApiError，带上状态码，方便界面区分"没连上"和"接口报错"。 */
export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new ApiError(`${path} 返回 ${response.status}`, response.status);
  }
  return (await response.json()) as T;
}

/**
 * 健康检查的返回。
 *
 * 直接取自生成的类型，不手写。后端改了字段，`npm run gen:api` 一跑，
 * 用错字段的地方立刻编译不过 —— 这正是要的效果。
 */
export type Health = components["schemas"]["HealthResponse"];
