import { NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import {
  createReauthToken,
  REAUTH_COOKIE,
  REAUTH_TTL_SECONDS,
  verifyPassword,
} from "@/lib/auth-core";
import { writeAudit } from "@/lib/audit";
import { clientAddress, validRequestOrigin } from "@/lib/security";

type Attempt = { failures: number; blockedUntil: number };
const attempts = new Map<string, Attempt>();

export async function POST(request: Request) {
  const session = await getAdminSession();
  if (!session) return NextResponse.json({ detail: "unauthorized" }, { status: 401 });
  if (!validRequestOrigin(request)) {
    return NextResponse.json({ detail: "invalid request origin" }, { status: 403 });
  }
  const address = clientAddress(request);
  const current = attempts.get(address);
  if (current && current.blockedUntil > Date.now()) {
    return NextResponse.json({ detail: "too many reauthentication attempts" }, { status: 429 });
  }
  let body: { password?: string };
  try {
    body = (await request.json()) as { password?: string };
  } catch {
    return NextResponse.json({ detail: "invalid JSON body" }, { status: 400 });
  }
  const valid = Boolean(body.password) && verifyPassword(
    body.password!,
    process.env.ADMIN_PASSWORD_HASH || "",
  );
  if (!valid) {
    const failures = (current?.failures || 0) + 1;
    attempts.set(address, {
      failures,
      blockedUntil: failures >= 5 ? Date.now() + 15 * 60 * 1000 : 0,
    });
    await writeAudit({
      username: session.username,
      address,
      action: "reauthenticate",
      resource: "sensitive-payloads",
      outcome: "failure",
      status: 401,
    });
    return NextResponse.json({ detail: "密码错误" }, { status: 401 });
  }
  attempts.delete(address);
  const secret = process.env.ADMIN_SESSION_SECRET || "";
  if (!secret) return NextResponse.json({ detail: "session secret is not configured" }, { status: 503 });
  const response = NextResponse.json({ ok: true, expires_in: REAUTH_TTL_SECONDS });
  response.cookies.set(REAUTH_COOKIE, createReauthToken(session.username, secret), {
    httpOnly: true,
    secure: true,
    sameSite: "strict",
    maxAge: REAUTH_TTL_SECONDS,
    path: "/admin",
  });
  await writeAudit({
    username: session.username,
    address,
    action: "reauthenticate",
    resource: "sensitive-payloads",
    outcome: "success",
    status: 200,
  });
  return response;
}
