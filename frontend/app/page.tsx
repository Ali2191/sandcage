import { Body, Main, Side } from "@/components/shell";
import { RecentRuns } from "@/components/recent-runs";
import { StartForm } from "@/components/start/start-form";
import { Card } from "@/components/ui";
import { COPY } from "@/lib/copy";
import Link from "next/link";

export default function StartPage() {
  return (
    <Body>
      <Main flush>
        <StartForm />
      </Main>
      <Side label="About this page">
        <Card as="section" className="p-[22px]">
          <h2 className="m-0 mb-4 text-base font-semibold text-head">{COPY.start.next.title}</h2>
          <ol className="m-0 list-none space-y-4 p-0">
            {COPY.start.next.steps.map((step, i) => (
              <li key={step.title} className="grid grid-cols-[28px_1fr] gap-x-3">
                <span className={`font-mono text-sm ${i === 2 ? "text-ember" : "text-faint"}`}>{String(i + 1).padStart(2, "0")}</span>
                <div>
                  <div className="font-semibold text-head">{step.title}</div>
                  <div className="text-sm text-muted">{step.body}</div>
                </div>
              </li>
            ))}
          </ol>
        </Card>
        <Card as="section" className="p-[22px]">
          <div className="mb-2 flex items-baseline justify-between gap-3">
            <h2 className="m-0 text-base font-semibold text-head">{COPY.runs.recent}</h2>
            <Link href="/runs" className="text-sm text-muted">
              {COPY.runs.all}
            </Link>
          </div>
          <RecentRuns compact />
        </Card>
      </Side>
    </Body>
  );
}
