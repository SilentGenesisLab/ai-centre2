import { randomUUID } from "node:crypto";
import { mkdir, rm, writeFile } from "node:fs/promises";
import { basename, extname } from "node:path";
import { NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { writeAudit } from "@/lib/audit";
import { backendUrl } from "@/lib/proxy-policy";
import { clientAddress, validRequestOrigin } from "@/lib/security";

const ALLOWED = new Set([".png", ".jpg", ".jpeg", ".webp", ".bmp"]);
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;

export async function POST(request: Request) {
  const session = await getAdminSession();
  if (!session) return NextResponse.json({ detail: "unauthorized" }, { status: 401 });
  if (!validRequestOrigin(request)) {
    return NextResponse.json({ detail: "invalid request origin" }, { status: 403 });
  }
  const data = await request.formData();
  const images = data.getAll("images").filter((value): value is File => value instanceof File);
  if (!images.length || images.length > 20) {
    return NextResponse.json({ detail: "upload between 1 and 20 images" }, { status: 422 });
  }
  const requestId = randomUUID();
  const root = process.env.ADMIN_UPLOAD_DIR || "/home/donxu/ai-centre/runtime/admin/uploads";
  const directory = `${root.replace(/[\\/]$/, "")}/${requestId}`;
  await mkdir(directory, { recursive: true });
  try {
    const inputs = [];
    for (const [index, image] of images.entries()) {
      const suffix = extname(image.name).toLowerCase();
      if (!ALLOWED.has(suffix)) {
        return NextResponse.json({ detail: `unsupported image type: ${suffix}` }, { status: 415 });
      }
      if (!image.size || image.size > MAX_IMAGE_BYTES) {
        return NextResponse.json({ detail: "each image must be between 1 byte and 20 MiB" }, { status: 413 });
      }
      const filename = `${String(index + 1).padStart(2, "0")}-${basename(image.name).replace(/[^A-Za-z0-9._-]/g, "_")}`;
      const path = `${directory}/${filename}`;
      await writeFile(path, Buffer.from(await image.arrayBuffer()), { mode: 0o600 });
      inputs.push({ image_id: `${requestId}-${index + 1}`, path, regions: [] });
    }
    const token = process.env.AI_CENTRE_SERVICE_TOKEN || process.env.SERVICE_TOKEN || "";
    if (!token) return NextResponse.json({ detail: "service token is not configured" }, { status: 503 });
    const upstream = await fetch(`${backendUrl("control")}/internal/admin/ocr/batch`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
      body: JSON.stringify({
        job_id: requestId,
        source_lang_hint: String(data.get("source_lang_hint") || "") || null,
        images: inputs,
      }),
    });
    const body = await upstream.text();
    await writeAudit({
      username: session.username,
      address: clientAddress(request),
      action: "ocr",
      resource: `ocr:${requestId}`,
      outcome: upstream.ok ? "success" : "failure",
      status: upstream.status,
    });
    return new Response(body, {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") || "application/json" },
    });
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}
