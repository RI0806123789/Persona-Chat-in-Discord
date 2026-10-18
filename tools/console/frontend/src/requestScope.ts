/** 接続の変更と取得順序を照合し、遅れて返った古い結果を捨てる。 */
export class RequestScope {
  private generation = 0;
  private requests = new Map<string, number>();

  invalidate(): void {
    this.generation += 1;
    this.requests.clear();
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
