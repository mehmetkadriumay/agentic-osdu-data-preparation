import type { ToolResponse } from "./types.ts";

const actor = { actor_id: "local-user" };

export function envelope<T>(workspaceId: string, input: T): object {
  return {
    request_id: crypto.randomUUID(),
    workspace_id: workspaceId,
    actor,
    input,
  };
}

export async function postTool<T>(path: string, body: object): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = await response.json() as ToolResponse<T>;
  if (!response.ok || !result.output) {
    throw new Error(result.errors?.[0]?.message ?? `HTTP ${response.status}`);
  }
  return result.output;
}
