/**
 * 后端 API 的访问入口。
 *
 * 类型不手写：跑 `npm run gen:api` 从后端的 OpenAPI 生成到 `src/lib/api-types.ts`。
 * 手写类型一定会和后端漂移，而且漂移了不会报错，只会在运行时拿到 undefined。
 *
 * 管理员令牌**按请求传入**（`token` 参数），不从构建期环境变量读：
 * `NEXT_PUBLIC_*` 会被打进浏览器可见的 JS，而 compose 部署只在构建时传
 * `NEXT_PUBLIC_API_BASE`，令牌根本到不了页面。令牌由 `lib/admin-token.ts`
 * 在浏览器里保管（sessionStorage），写接口的调用方把它带过来。
 */

import type { components } from "./api-types";

/** 后端地址。开发时后端跑在 8000，前端跑在 3000，跨域由后端放行。 */
export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

/** 后端的统一错误形状（backend/app/api/errors.py）。 */
type ErrorBody = components["schemas"]["ErrorResponse"];

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    /** 后端的机器可读错误码，如 BENCHMARK_SET_NOT_FOUND。解析不出时为 undefined。 */
    readonly code?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * 把 catch 到的任意东西翻成给人看的一句话。
 *
 * 带上后端的 `code`：那句话是写给人看的，`code` 是写给日志和工单看的，
 * 两个都给出来，排查时不用再去翻 Network 面板。
 */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    return err.code ? `${err.message}（${err.code}）` : err.message;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}

/** 把失败响应翻成 ApiError：优先用后端的 message/code，解析不出就退回状态码。 */
async function toApiError(path: string, response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as Partial<ErrorBody>;
    return new ApiError(
      body.message ?? `${path} 返回 ${response.status}`,
      response.status,
      body.code ?? undefined,
    );
  } catch {
    return new ApiError(`${path} 返回 ${response.status}`, response.status);
  }
}

type RequestOptions = {
  method?: "GET" | "POST";
  /** 管理员令牌，只在需要保护的接口上传递（`X-Bench-Token` 头）。 */
  token?: string;
  body?: unknown;
};

/**
 * 统一发送 JSON 请求。
 *
 * 没配令牌也照样发出去，让后端返回 401 —— 后端才是权威，
 * 前端提前拦下来只会造出"两边判断不一致"的第二种真相。
 */
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
    throw await toApiError(path, response);
  }
  return (await response.json()) as T;
}

/** 发一个 GET 请求。失败时抛 ApiError，带上状态码，方便界面区分"没连上"和"接口报错"。 */
export async function apiGet<T>(path: string, token?: string): Promise<T> {
  return apiRequest<T>(path, { token });
}

/** 发一个 POST JSON 请求（写接口专用）。 */
export async function apiPost<T>(
  path: string,
  body?: unknown,
  token?: string,
): Promise<T> {
  return apiRequest<T>(path, { method: "POST", token, body });
}

/**
 * 拉一段文本制品（补丁正文、日志、轨迹）。
 *
 * 和 `apiGet` 的唯一区别是不解析 JSON。制品端点的响应有两种形态
 * （`backend/app/api/task_runs.py` 模块注释）：本地存储流式转发文本，
 * MinIO 返回 302 到签名 URL —— 两种情况 `fetch` 都会做成一个普通响应，
 * 302 会跟着跳转，最后拿到的都是文本。
 *
 * 调用方**不要轮询**：一份制品生成之后就不再改动，内容哈希是它的身份证。
 */
export async function apiText(path: string, token?: string): Promise<string> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      Accept: "*/*",
      ...(token ? { "X-Bench-Token": token } : {}),
    },
    cache: "no-store",
  });
  if (!response.ok) {
    throw await toApiError(path, response);
  }
  return await response.text();
}

/**
 * 健康检查的返回。
 *
 * 直接取自生成的类型，不手写。后端改了字段，`npm run gen:api` 一跑，
 * 用错字段的地方立刻编译不过 —— 这正是要的效果。
 */
export type Health = components["schemas"]["HealthResponse"];
