import { NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { writeAudit } from "@/lib/audit";
import { backendUrl } from "@/lib/proxy-policy";
import { hasRecentReauthentication } from "@/lib/reauth";
import { clientAddress } from "@/lib/security";

type Context = { params: Promise<{ callId: string }> };

export async function GET(request: Request, context: Context) {
  const session = await getAdminSession();
  if (!session) return NextResponse.json({ detail: "unauthorized" }, { status: 401 });
  if (!(await hasRecentReauthentication(session.username))) {
    return NextResponse.json({ detail: "recent password verification required" }, { status: 403 });
  }
  const { callId } = await context.params;
  if (!/^[0-9a-f-]{36}$/i.test(callId)) {
    return NextResponse.json({ detail: "invalid call id" }, { status: 422 });
  }
  const token = process.env.AI_CENTRE_SERVICE_TOKEN || process.env.SERVICE_TOKEN || "";
  if (!token) return NextResponse.json({ detail: "service token is not configured" }, { status: 503 });
  let upstream: Response;
  try {
    upstream = await fetch(
      `${backendUrl("control")}/internal/admin/observability/calls/${callId}/reveal`,
      { headers: { authorization: `Bearer ${token}` }, cache: "no-store" },
    );
  } catch {
    return NextResponse.json({ detail: "control plane is unavailable" }, { status: 503 });
  }
  await writeAudit({
    username: session.username,
    address: clientAddress(request),
    action: "reveal",
    resource: `api-call:${callId}`,
    outcome: upstream.ok ? "success" : "failure",
    status: upstream.status,
  });
  return new Response(upstream.body, {
    status: upstream.status,
    headers: { "content-type": upstream.headers.get("content-type") || "application/json", "cache-control": "no-store" },
  });
}
