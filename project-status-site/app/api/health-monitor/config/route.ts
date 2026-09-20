import { NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { writeAudit } from "@/lib/audit";
import { backendUrl } from "@/lib/proxy-policy";
import { hasRecentReauthentication } from "@/lib/reauth";
import { clientAddress, validRequestOrigin } from "@/lib/security";

async function mutate(request: Request, action: "config" | "test-notification") {
  const session = await getAdminSession();
  if (!session) return NextResponse.json({ detail: "unauthorized" }, { status: 401 });
  if (!validRequestOrigin(request)) return NextResponse.json({ detail: "invalid request origin" }, { status: 403 });
  if (!(await hasRecentReauthentication(session.username))) {
    return NextResponse.json({ detail: "recent password verification required" }, { status: 403 });
  }
  const token = process.env.AI_CENTRE_SERVICE_TOKEN || process.env.SERVICE_TOKEN || "";
  if (!token) return NextResponse.json({ detail: "service token is not configured" }, { status: 503 });
  const body = action === "config" ? await request.text() : undefined;
  let upstream: Response;
  try {
    upstream = await fetch(`${backendUrl("control")}/internal/admin/health-monitor/${action}`, {
      method: action === "config" ? "PUT" : "POST",
      headers: { authorization: `Bearer ${token}`, ...(body ? { "content-type": "application/json" } : {}) },
      body,
      cache: "no-store",
    });
  } catch {
    return NextResponse.json({ detail: "control plane is unavailable" }, { status: 503 });
  }
  await writeAudit({ username: session.username, address: clientAddress(request), action: action === "config" ? "update" : "test",
    resource: `health-monitor:${action}`, outcome: upstream.ok ? "success" : "failure", status: upstream.status });
  return new Response(upstream.body, { status: upstream.status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
}

export async function PUT(request: Request) { return mutate(request, "config"); }
export async function POST(request: Request) { return mutate(request, "test-notification"); }
