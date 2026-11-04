/**
 * The report's outcome pill, headline and declined line, looked up in the table the
 * backend generates (`contract/samples/report-wording.json`, written by its own wording
 * functions). The mock never writes this wording itself, so it cannot drift from the API.
 */
import { readFileSync } from "node:fs";
import path from "node:path";

import type { RunStatus } from "../../contract/events.ts";

interface ReportRow {
  status: RunStatus;
  withheld: boolean;
  blocked: number;
  outcome: string;
  headline: string;
}

interface DeclinedRow {
  declined: number;
  declined_line: string | null;
}

interface WordingTable {
  reports: ReportRow[];
  declined: DeclinedRow[];
}

const WORDING_PATH = path.join(import.meta.dirname, "../../contract/samples/report-wording.json");
const TABLE: WordingTable = JSON.parse(readFileSync(WORDING_PATH, "utf-8")) as WordingTable;

export function reportRow(status: RunStatus, blocked: number, withheld: boolean): ReportRow {
  const row = TABLE.reports.find((r) => r.status === status && r.withheld === withheld && r.blocked === blocked);
  if (!row) throw new Error(`No report wording for ${status}, blocked=${blocked}, withheld=${withheld}`);
  return row;
}

export function declinedLine(declined: number): string | null {
  const row = TABLE.declined.find((r) => r.declined === declined);
  if (!row) throw new Error(`No declined wording for ${declined}`);
  return row.declined_line;
}
