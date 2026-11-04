import type { Metadata } from "next";
import { Centered } from "@/components/shell";
import { Policy } from "@/components/policy/policy";

export const metadata: Metadata = { title: "Policy" };

export default function PolicyPage() {
  return (
    <Centered max={880}>
      <Policy />
    </Centered>
  );
}
