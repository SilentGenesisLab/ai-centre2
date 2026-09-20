import { NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { readAudit } from "@/lib/audit";

export async function GET() {
  const session = await getAdminSession();
  if (!session) return NextResponse.json({ detail: "unauthorized" }, { status: 401 });
  return NextResponse.json({ events: await readAudit(200) });
}
