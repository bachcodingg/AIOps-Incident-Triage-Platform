import { describe as group, expect, it } from "vitest";

import { actionText, ago, describe, duration, status } from "./format";

group("format", () => {
  it("says how long ago, in the unit that reads best", () => {
    expect(ago(1000, 1030)).toBe("30s ago");
    expect(ago(1000, 1000 + 600)).toBe("10 min ago");
    expect(ago(1000, 1000 + 3 * 3600)).toBe("3.0 h ago");
  });

  it("names actions as an operator would", () => {
    expect(actionText("scale_out", "gateway")).toBe("scale out gateway");
    expect(actionText("none_escalate", null)).toBe("escalate to a human");
    expect(actionText(null, null)).toBe("–");
  });

  it("turns audit entries into a timeline", () => {
    expect(describe({ event: "approved", actor: "alice", data: {} })).toBe("approved by alice");
    expect(describe({ event: "executed", actor: "remediator", data: { by: "alice", dry_run: true } })).toContain("dry run");
    expect(describe({ event: "resolved", actor: "loop", data: { time_to_recover_s: 540 } })).toBe("resolved after 9.0 min");
    expect(duration(45)).toBe("45 s");
  });

  it("marks what needs a human", () => {
    expect(status("awaiting-approval").tone).toBe("act");
    expect(status("something-new").label).toBe("something-new");
  });
});
