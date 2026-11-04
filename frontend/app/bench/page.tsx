import type { Metadata } from "next";
import { Bench } from "@/components/bench/bench";
import { Centered } from "@/components/shell";

export const metadata: Metadata = { title: "Bench" };

export default function BenchPage() {
  return (
    <Centered max={900}>
      <Bench />
    </Centered>
  );
}
