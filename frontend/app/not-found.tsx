import Link from "next/link";
import { Centered } from "@/components/shell";
import { COPY } from "@/lib/copy";

export default function NotFound() {
  return (
    <Centered max={512}>
      <div className="pt-16">
        <h1 className="m-0 text-3xl font-bold text-head">{COPY.errors.notFound.title}</h1>
        <p className="mt-3 text-muted">{COPY.errors.notFound.body}</p>
        <Link href="/" className="font-bold">
          {COPY.errors.notFound.link}
        </Link>
      </div>
    </Centered>
  );
}
