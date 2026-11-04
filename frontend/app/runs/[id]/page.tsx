import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { RunView } from "@/components/run/run-view";

export const metadata: Metadata = { title: "Run" };

/** Run ids are letters, digits, "_" and "-", up to 64 characters. Anything else is a 404. */
const VALID_ID = /^[A-Za-z0-9_-]{1,64}$/;

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!VALID_ID.test(id)) notFound();
  return <RunView runId={id} />;
}
