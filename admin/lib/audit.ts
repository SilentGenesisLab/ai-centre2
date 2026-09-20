import { appendFile, mkdir, readFile } from "node:fs/promises";
import { dirname } from "node:path";
import { randomUUID } from "node:crypto";

export type AuditEvent = {
  id: string;
  timestamp: string;
  username: string;
  address: string;
  action: string;
  resource: string;
  outcome: "success" | "failure";
  status?: number;
};

function auditPath(): string {
  return process.env.ADMIN_AUDIT_LOG || "/home/donxu/ai-centre/runtime/admin/audit.jsonl";
}

export async function writeAudit(event: Omit<AuditEvent, "id" | "timestamp">): Promise<void> {
  const record: AuditEvent = {
    id: randomUUID(),
    timestamp: new Date().toISOString(),
    ...event,
  };
  const path = auditPath();
  await mkdir(dirname(path), { recursive: true });
  await appendFile(/* turbopackIgnore: true */ path, `${JSON.stringify(record)}\n`, { encoding: "utf8", mode: 0o600 });
}

export async function readAudit(limit = 200): Promise<AuditEvent[]> {
  try {
    const content = await readFile(/* turbopackIgnore: true */ auditPath(), "utf8");
    return content
      .trim()
      .split("\n")
      .filter(Boolean)
      .slice(-limit)
      .reverse()
      .map((line) => JSON.parse(line) as AuditEvent);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  }
}
