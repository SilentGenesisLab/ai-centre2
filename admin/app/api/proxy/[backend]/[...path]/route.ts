import { NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { writeAudit } from "@/lib/audit";
import { backendUrl, isAllowedProxyRequest, type BackendName } from "@/lib/proxy-policy";
import { clientAddress, validRequestOrigin } from "@/lib/security";

const MAX_UPLOAD_BYTES = 512 * 1024 * 1024;

type RouteContext = { params: Promise<{ backend: string; path: string[] }> };

async function forward(request: Request, context: RouteContext): Promise<Response> {
  const session = await getAdminSession();
  if (!session) return NextResponse.json({ detail: "unauthorized" }, { status: 401 });
  if (!validRequestOrigin(request)) {
    return NextResponse.json({ detail: "invalid request origin" }, { status: 403 });
  }
  const { backend, path: segments } = await context.params;
  const path = `/${segments.join("/")}`;
  if (!isAllowedProxyRequest(backend, request.method, path)) {
    return NextResponse.json({ detail: "proxy route is not allowed" }, { status: 404 });
  }
  const contentLength = Number(request.headers.get("content-length") || 0);
  if (contentLength > MAX_UPLOAD_BYTES) {
    return NextResponse.json({ detail: "upload is too large" }, { status: 413 });
  }
  const target = new URL(path, `${backendUrl(backend as BackendName).replace(/\/$/, "")}/`);
  target.search = new URL(request.url).search;
  const headers = new Headers();
  for (const name of ["accept", "content-type", "range", "if-none-match"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (backend === "control" || backend === "face") {
    const token = process.env.AI_CENTRE_SERVICE_TOKEN || process.env.SERVICE_TOKEN || "";
    if (!token) return NextResponse.json({ detail: "service token is not configured" }, { status: 503 });
    headers.set("authorization", `Bearer ${token}`);
  }
  const init: RequestInit & { duplex?: "half" } = {
    method: request.method,
    headers,
    redirect: "manual",
  };
  if (!new Set(["GET", "HEAD"]).has(request.method)) {
    init.body = request.body;
    init.duplex = "half";
  }
  let upstream: Response;
  try {
    upstream = await fetch(target, init);
  } catch {
    return NextResponse.json({ detail: `${backend} backend is unavailable` }, { status: 503 });
  }
  const responseHeaders = new Headers();
  for (const name of ["content-type", "content-disposition", "content-length", "x-request-id", "x-tts-provider", "x-audio-duration-ms", "x-audio-sample-rate", "x-audio-channels", "x-audio-sample-format", "x-tts-request-id", "x-tts-quality-status", "x-tts-segment-count", "x-tts-emotion-enhancement", "x-ocr-worker"]) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  responseHeaders.set("cache-control", "no-store");
  if (!["GET", "HEAD"].includes(request.method)) {
    await writeAudit({
      username: session.username,
      address: clientAddress(request),
      action: request.method.toLowerCase(),
      resource: `${backend}:${path}`,
      outcome: upstream.ok ? "success" : "failure",
      status: upstream.status,
    });
  }
  return new Response(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

export { forward as GET, forward as POST, forward as PUT, forward as PATCH, forward as DELETE };
