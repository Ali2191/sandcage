"use client";

import { useEffect } from "react";
import { Centered } from "@/components/shell";
import { Button } from "@/components/ui";
import { COPY } from "@/lib/copy";

export default function ErrorPage({ error, unstable_retry }: { error: Error & { digest?: string }; unstable_retry: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);
  return (
    <Centered max={512}>
      <div className="pt-16">
        <h1 className="m-0 text-3xl font-bold text-head">{COPY.errors.crash.title}</h1>
        <p className="mb-6 mt-3 break-words text-muted">{error.message || COPY.errors.crash.fallback}</p>
        <Button onClick={() => unstable_retry()}>{COPY.errors.crash.retry}</Button>
      </div>
    </Centered>
  );
}
