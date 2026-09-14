import { afterEach, describe, expect, it, vi } from "vitest";

import { RefreshScheduler } from "../src/refresh-scheduler";

afterEach(() => {
  vi.useRealTimers();
});

describe("RefreshScheduler", () => {
  it("debounces edit bursts while enforcing the maximum batch delay", async () => {
    vi.useFakeTimers();
    const runs: string[] = [];
    const scheduler = new RefreshScheduler(async (reason) => {
      runs.push(reason);
    }, 100, 300);

    scheduler.hint();
    await vi.advanceTimersByTimeAsync(80);
    scheduler.hint();
    await vi.advanceTimersByTimeAsync(80);
    scheduler.hint();
    await vi.advanceTimersByTimeAsync(80);
    expect(runs).toEqual([]);
    await vi.advanceTimersByTimeAsync(60);

    expect(runs).toEqual(["automatic"]);
    scheduler.dispose();
  });

  it("coalesces edits arriving during a refresh and supports manual refresh", async () => {
    vi.useFakeTimers();
    const releases: Array<() => void> = [];
    const runs: string[] = [];
    const scheduler = new RefreshScheduler(async (reason) => {
      runs.push(reason);
      await new Promise<void>((resolve) => {
        releases.push(resolve);
      });
    }, 50, 200);

    scheduler.hint();
    await vi.advanceTimersByTimeAsync(50);
    scheduler.hint();
    expect(runs).toEqual(["automatic"]);
    releases.shift()?.();
    await Promise.resolve();
    await Promise.resolve();
    expect(runs).toEqual(["automatic", "automatic"]);
    const manual = scheduler.manual();
    releases.shift()?.();
    await Promise.resolve();
    await Promise.resolve();
    expect(runs).toEqual(["automatic", "automatic", "manual"]);
    releases.shift()?.();
    await manual;
    scheduler.dispose();
  });
});
