import { notFound } from "next/navigation";
import { requireAdminSession } from "@/lib/auth";
import { AdminConsole } from "@/components/admin-console";
import { SECTIONS, type SectionId } from "@/lib/sections";

export const dynamic = "force-dynamic";

export async function generateMetadata({ params }: { params: Promise<{ section: string }> }) {
  const { section } = await params;
  const item = SECTIONS.find((candidate) => candidate.id === section);
  return { title: item?.label || "管理后台" };
}

export default async function SectionPage({ params }: { params: Promise<{ section: string }> }) {
  const session = await requireAdminSession();
  const { section } = await params;
  if (!SECTIONS.some((candidate) => candidate.id === section)) notFound();
  return <AdminConsole section={section as SectionId} username={session.username} />;
}
