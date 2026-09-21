"use client";

/**
 * 管理员令牌在浏览器里的保管处。
 *
 * P0 只有一个管理员身份，没有用户体系（§14.4）：写接口（建实验、取消、重试、
 * 人工复核）靠 `X-Bench-Token` 头，值和后端 `.env` 里的 `ADMIN_TOKEN` 是同一个串。
 *
 * 为什么放 sessionStorage、而不是构建期的 `NEXT_PUBLIC_ADMIN_TOKEN`：
 * - `NEXT_PUBLIC_*` 会被打进所有人都下载得到的 JS，等于把密钥发布出去；
 * - compose 部署（E10-T1）构建镜像时只传 `NEXT_PUBLIC_API_BASE`，令牌根本到不了页面，
 *   写按钮在部署环境里会一律 401。
 *
 * sessionStorage 只活在当前标签页：关掉就没了，换个标签页要重输。这是有意的 ——
 * 令牌不该在浏览器里长期躺着。私密窗口或禁用存储时读写都可能抛错，全部 try/catch，
 * 退化成"只在内存里、刷新即丢"。
 *
 * 用 `useSyncExternalStore` 而不是每个组件各自 `useState`：同一页上可能有两处
 * 需要令牌（比如 Run Detail 的取消和重试按钮），在一处输入另一处要立刻能用。
 * 服务端快照固定为空串，避免水合时和浏览器里存的值对不上。
 */

import { useSyncExternalStore } from "react";

const STORAGE_KEY = "bench.admin-token";

const listeners = new Set<() => void>();

/** 内存里的当前值；`null` 表示还没从 sessionStorage 读过。 */
let cached: string | null = null;

function readStored(): string {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function snapshot(): string {
  if (cached === null) cached = readStored();
  return cached;
}

function serverSnapshot(): string {
  return "";
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** 写入令牌；空串等于清除。 */
export function setAdminToken(next: string): void {
  cached = next;
  try {
    if (next === "") window.sessionStorage.removeItem(STORAGE_KEY);
    else window.sessionStorage.setItem(STORAGE_KEY, next);
  } catch {
    // 存不进去就只留在内存里
  }
  for (const listener of listeners) listener();
}

/** 当前令牌（没设置时是空串）和写入函数。 */
export function useAdminToken(): readonly [string, (next: string) => void] {
  const token = useSyncExternalStore(subscribe, snapshot, serverSnapshot);
  return [token, setAdminToken] as const;
}
