import { NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { REAUTH_COOKIE, SESSION_COOKIE } from "@/lib/auth-core";
import { writeAudit } from "@/lib/audit";
import { clientAddress, validRequestOrigin } from "@/lib/security";

export async function POST(request: Request) {
  if (!validRequestOrigin(request)) {
    return NextResponse.json({ detail: "invalid request origin" }, { status: 403 });
  }
  const session = await getAdminSession();
  const response = NextResponse.json({ ok: true });
  response.cookies.set(SESSION_COOKIE, "", {
    httpOnly: true,
    secure: true,
    sameSite: "strict",
    maxAge: 0,
    path: "/admin",
  });
  response.cookies.set(REAUTH_COOKIE, "", {
    httpOnly: true,
    secure: true,
    sameSite: "strict",
    maxAge: 0,
    path: "/admin",
  });
  if (session) {
    await writeAudit({
      username: session.username,
      address: clientAddress(request),
      action: "logout",
      resource: "admin",
      outcome: "success",
      status: 200,
    });
  }
  return response;
}
