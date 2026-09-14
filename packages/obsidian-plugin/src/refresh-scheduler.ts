export type RefreshReason = "automatic" | "manual";

export class RefreshScheduler {
  readonly #run: (reason: RefreshReason) => Promise<void>;
  readonly #debounceMs: number;
  readonly #maximumDelayMs: number;
  #debounceTimer: ReturnType<typeof setTimeout> | null = null;
  #maximumTimer: ReturnType<typeof setTimeout> | null = null;
  #active: Promise<void> | null = null;
  #pendingReason: RefreshReason | null = null;
  #disposed = false;

  public constructor(
    run: (reason: RefreshReason) => Promise<void>,
    debounceMs = 1_000,
    maximumDelayMs = 5_000,
  ) {
    if (debounceMs < 1 || maximumDelayMs < debounceMs) {
      throw new Error("invalid refresh timing");
    }
    this.#run = run;
    this.#debounceMs = debounceMs;
    this.#maximumDelayMs = maximumDelayMs;
  }

  public hint(): void {
    if (this.#disposed) return;
    this.#pendingReason ??= "automatic";
    if (this.#debounceTimer !== null) clearTimeout(this.#debounceTimer);
    this.#debounceTimer = setTimeout(() => void this.#drain(), this.#debounceMs);
    if (this.#maximumTimer === null) {
      this.#maximumTimer = setTimeout(() => void this.#drain(), this.#maximumDelayMs);
    }
  }

  public async manual(): Promise<void> {
    if (this.#disposed) return;
    this.#pendingReason = "manual";
    this.#clearTimers();
    await this.#drain();
  }

  public dispose(): void {
    this.#disposed = true;
    this.#pendingReason = null;
    this.#clearTimers();
  }

  async #drain(): Promise<void> {
    this.#clearTimers();
    if (this.#disposed) return;
    if (this.#active !== null) {
      await this.#active;
      return;
    }
    const active = this.#runPending();
    this.#active = active;
    try {
      await active;
    } finally {
      if (this.#active === active) this.#active = null;
      if (this.#pendingReason !== null && !this.#disposed) void this.#drain();
    }
  }

  async #runPending(): Promise<void> {
    while (this.#pendingReason !== null && !this.#disposed) {
      const reason = this.#pendingReason;
      this.#pendingReason = null;
      await this.#run(reason);
    }
  }

  #clearTimers(): void {
    if (this.#debounceTimer !== null) clearTimeout(this.#debounceTimer);
    if (this.#maximumTimer !== null) clearTimeout(this.#maximumTimer);
    this.#debounceTimer = null;
    this.#maximumTimer = null;
  }
}
