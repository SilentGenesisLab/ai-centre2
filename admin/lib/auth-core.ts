import {
  createHmac,
  randomBytes,
  scryptSync,
  timingSafeEqual,
} from "node:crypto";

export const SESSION_COOKIE = "ai_centre_admin";
export const SESSION_TTL_SECONDS = 8 * 60 * 60;
export const REAUTH_COOKIE = "ai_centre_admin_reauth";
export const REAUTH_TTL_SECONDS = 10 * 60;

export type AdminSession = {
  username: string;
  expiresAt: number;
};

export function hashPassword(password: string, salt = randomBytes(16).toString("hex")): string {
  const digest = scryptSync(password, salt, 64).toString("hex");
  return `${salt}:${digest}`;
}

export function verifyPassword(password: string, stored: string): boolean {
  const [salt, expectedHex] = stored.split(":", 2);
  if (!salt || !expectedHex || !/^[a-f0-9]{128}$/i.test(expectedHex)) return false;
  const actual = scryptSync(password, salt, 64);
  const expected = Buffer.from(expectedHex, "hex");
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

function signature(payload: string, secret: string): string {
  return createHmac("sha256", secret).update(payload).digest("base64url");
}

export function createSessionToken(
  username: string,
  secret: string,
  nowMs = Date.now(),
): string {
  const session: AdminSession = {
    username,
    expiresAt: Math.floor(nowMs / 1000) + SESSION_TTL_SECONDS,
  };
  const payload = Buffer.from(JSON.stringify(session)).toString("base64url");
  return `${payload}.${signature(payload, secret)}`;
}

export function createReauthToken(username: string, secret: string, nowMs = Date.now()): string {
  const session: AdminSession = {
    username,
    expiresAt: Math.floor(nowMs / 1000) + REAUTH_TTL_SECONDS,
  };
  const payload = Buffer.from(JSON.stringify(session)).toString("base64url");
  return `${payload}.${signature(payload, `${secret}:reauth`)}`;
}

export function verifyReauthToken(
  token: string | undefined,
  secret: string,
  nowMs = Date.now(),
): AdminSession | null {
  return verifySessionToken(token, `${secret}:reauth`, nowMs);
}

export function verifySessionToken(
  token: string | undefined,
  secret: string,
  nowMs = Date.now(),
): AdminSession | null {
  if (!token || !secret) return null;
  const [payload, suppliedSignature] = token.split(".", 2);
  if (!payload || !suppliedSignature) return null;
  const expectedSignature = signature(payload, secret);
  const supplied = Buffer.from(suppliedSignature);
  const expected = Buffer.from(expectedSignature);
  if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) return null;
  try {
    const session = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as AdminSession;
    if (!session.username || session.expiresAt <= Math.floor(nowMs / 1000)) return null;
    return session;
  } catch {
    return null;
  }
}
