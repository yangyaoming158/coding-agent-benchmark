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
    readonly code = "HTTP_ERROR",
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type RequestOptions = {
  method?: "GET" | "POST";
  token?: string;
  body?: unknown;
};

/** 统一发送 JSON 请求。管理员 token 只在需要保护的接口上传递。 */
export async function apiRequest<T>(
  path: string,
  { method = "GET", token, body }: RequestOptions = {},
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      Accept: "application/json",
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(token ? { "X-Bench-Token": token } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as {
      code?: string;
      message?: string;
    } | null;
    throw new ApiError(
      payload?.message ?? `${path} 返回 ${response.status}`,
      response.status,
      payload?.code,
    );
  }
  return (await response.json()) as T;
}

/** 发一个 GET 请求。 */
export async function apiGet<T>(path: string, token?: string): Promise<T> {
  return apiRequest<T>(path, { token });
}

/** 发一个 POST JSON 请求。 */
export async function apiPost<T>(
  path: string,
  body: unknown,
  token?: string,
): Promise<T> {
  return apiRequest<T>(path, { method: "POST", token, body });
}

/** 按需读取补丁或日志正文；大制品不经过 JSON。 */
export async function apiText(path: string, token?: string): Promise<string> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: token ? { "X-Bench-Token": token } : undefined,
    cache: "no-store",
  });
  if (!response.ok) {
    throw new ApiError(`${path} 返回 ${response.status}`, response.status);
  }
  return response.text();
}

/**
 * 健康检查的返回。
 *
 * 直接取自生成的类型，不手写。后端改了字段，`npm run gen:api` 一跑，
 * 用错字段的地方立刻编译不过 —— 这正是要的效果。
 */
export type Health = components["schemas"]["HealthResponse"];
