/** 接続の変更と取得順序を照合し、遅れて返った古い結果を捨てる。 */
export class RequestScope {
  private generation = 0;
  private timers = new Set<ReturnType<typeof window.setTimeout>>();
  private requests = new Map<string, number>();

  invalidate(): void {
    for (const timer of this.timers) window.clearTimeout(timer);
    this.timers.clear();
    this.generation += 1;
    this.requests.clear();
  }

  schedule(callback: () => void, delay: number): void {
    const current = this.current();
    const timer = window.setTimeout(() => {
      this.timers.delete(timer);
      if (current()) callback();
    }, delay);
    this.timers.add(timer);
  }

  current(): () => boolean {
    const generation = this.generation;
    return () => generation === this.generation;
  }

  start(kind: string): () => boolean {
    const current = this.current();
    const request = (this.requests.get(kind) ?? 0) + 1;
    this.requests.set(kind, request);
    return () => current() && this.requests.get(kind) === request;
  }
}
