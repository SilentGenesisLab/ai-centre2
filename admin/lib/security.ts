const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export function validRequestOrigin(request: Request): boolean {
  if (SAFE_METHODS.has(request.method.toUpperCase())) return true;
  const origin = request.headers.get("origin");
  if (!origin) return false;
  const expected = process.env.ADMIN_PUBLIC_ORIGIN || new URL(request.url).origin;
  return origin === expected;
}

export function clientAddress(request: Request): string {
  return (
    request.headers.get("x-forwarded-for")?.split(",", 1)[0]?.trim() ||
    request.headers.get("x-real-ip") ||
    "unknown"
  );
}
