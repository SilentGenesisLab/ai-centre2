import { cookies } from "next/headers";
import { REAUTH_COOKIE, verifyReauthToken } from "./auth-core";

export async function hasRecentReauthentication(username: string): Promise<boolean> {
  const store = await cookies();
  const token = store.get(REAUTH_COOKIE)?.value;
  const session = verifyReauthToken(token, process.env.ADMIN_SESSION_SECRET || "");
  return session?.username === username;
}
