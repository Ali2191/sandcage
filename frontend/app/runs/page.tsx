import type { Metadata } from "next";
import { RecentRuns } from "@/components/recent-runs";
import { Centered } from "@/components/shell";
import { Card } from "@/components/ui";
import { COPY } from "@/lib/copy";

export const metadata: Metadata = { title: "Runs" };

export default function RunsPage() {
  return (
    <Centered>
      <h1 className="m-0 text-[34px] font-bold tracking-tight text-head">{COPY.runs.headline}</h1>
      <p className="mb-7 mt-3 max-w-[62ch] text-[17px] text-muted">{COPY.runs.sub}</p>
      <Card className="px-6 py-3">
        <RecentRuns />
      </Card>
    </Centered>
  );
}
