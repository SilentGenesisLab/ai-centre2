import { NextResponse } from "next/server";
import {
  createSessionToken,
  SESSION_COOKIE,
  SESSION_TTL_SECONDS,
  verifyPassword,
} from "@/lib/auth-core";
import { writeAudit } from "@/lib/audit";
import { clientAddress, validRequestOrigin } from "@/lib/security";

type Attempt = { failures: number; blockedUntil: number };
const attempts = new Map<string, Attempt>();
const MAX_FAILURES = 5;
const BLOCK_MS = 15 * 60 * 1000;

export async function POST(request: Request) {
  const address = clientAddress(request);
  if (!validRequestOrigin(request)) {
    return NextResponse.json({ detail: "invalid request origin" }, { status: 403 });
  }
  const current = attempts.get(address);
  if (current && current.blockedUntil > Date.now()) {
    return NextResponse.json({ detail: "too many login attempts" }, { status: 429 });
  }
  let body: { username?: string; password?: string };
  try {
    body = (await request.json()) as { username?: string; password?: string };
  } catch {
    return NextResponse.json({ detail: "invalid JSON body" }, { status: 400 });
  }
  const configuredUser = process.env.ADMIN_USERNAME || "admin";
  const configuredHash = process.env.ADMIN_PASSWORD_HASH || "";
  const valid =
    body.username === configuredUser &&
    Boolean(body.password) &&
    Boolean(configuredHash) &&
    verifyPassword(body.password!, configuredHash);
  if (!valid) {
    const failures = (current?.failures || 0) + 1;
    attempts.set(address, {
      failures,
      blockedUntil: failures >= MAX_FAILURES ? Date.now() + BLOCK_MS : 0,
    });
    await writeAudit({
      username: body.username || "unknown",
      address,
      action: "login",
      resource: "admin",
      outcome: "failure",
      status: 401,
    });
    return NextResponse.json({ detail: "用户名或密码错误" }, { status: 401 });
  }
  attempts.delete(address);
  const secret = process.env.ADMIN_SESSION_SECRET || "";
  if (!secret) {
    return NextResponse.json({ detail: "admin session is not configured" }, { status: 503 });
  }
  const response = NextResponse.json({ username: configuredUser });
  response.cookies.set(
    SESSION_COOKIE,
    createSessionToken(configuredUser, secret),
    {
      httpOnly: true,
      secure: true,
      sameSite: "strict",
      maxAge: SESSION_TTL_SECONDS,
      path: "/admin",
    },
  );
  await writeAudit({
    username: configuredUser,
    address,
    action: "login",
    resource: "admin",
    outcome: "success",
    status: 200,
  });
  return response;
}
