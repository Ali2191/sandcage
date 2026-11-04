// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { StatusPill } from "./status-pill";

afterEach(cleanup);

describe("StatusPill", () => {
  it("says the run is waiting for you while connected", () => {
    render(<StatusPill connection="live" waiting />);
    expect(screen.getByRole("status").textContent).toContain("waiting for you");
  });

  it("says disconnected even when a question is open: a question the page cannot confirm is not live", () => {
    render(<StatusPill connection="disconnected" waiting />);
    expect(screen.getByRole("status").textContent).toBe("disconnected");
  });

  it("does not say waiting once the run finished", () => {
    render(<StatusPill connection="finished" waiting />);
    expect(screen.getByRole("status").textContent).not.toContain("waiting");
  });
});
