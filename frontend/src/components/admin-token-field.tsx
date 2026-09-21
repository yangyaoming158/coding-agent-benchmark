"use client";

import { useState } from "react";
import { useAdminToken } from "@/lib/admin-token";

/**
 * 管理员令牌输入框。放在每个有写按钮的地方旁边，不做全局登录页。
 *
 * 输入即保存（到本标签页的 sessionStorage），不要"确定"按钮 —— 令牌只有一处
 * 来源（后端 `.env` 的 `ADMIN_TOKEN`），粘贴进来就是全部动作。
 * 值本身不回显，只显示"已设置"，避免屏幕共享时把令牌亮出来。
 */
export function AdminTokenField({ hint }: { hint?: string }) {
  const [token, setToken] = useAdminToken();
  const [editing, setEditing] = useState(false);

  if (token !== "" && !editing) {
    return (
      <p className="text-xs text-neutral-500">
        管理员令牌已设置（仅本标签页有效）。
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="ml-2 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
        >
          换一个
        </button>
        <button
          type="button"
          onClick={() => setToken("")}
          className="ml-2 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
        >
          清除
        </button>
      </p>
    );
  }

  return (
    <label className="block text-xs text-neutral-600">
      管理员令牌
      <input
        type="password"
        autoComplete="off"
        value={token}
        onChange={(e) => setToken(e.target.value)}
        onBlur={() => setEditing(false)}
        placeholder="和后端 .env 里的 ADMIN_TOKEN 一致"
        className="mt-1 w-full max-w-md rounded-md border border-neutral-300 bg-white px-2 py-1.5 font-mono text-sm text-neutral-900 placeholder:font-sans placeholder:text-neutral-400"
      />
      <span className="mt-1 block text-neutral-500">
        {hint ?? "写接口（建实验、取消、重试）要带它；只存在本标签页，关掉即失效。"}
      </span>
    </label>
  );
}
