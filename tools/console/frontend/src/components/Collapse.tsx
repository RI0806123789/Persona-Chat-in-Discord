// 開閉をなめらかにする入れ物。高さを 0 から本来の高さへ（grid-template-rows の 0fr ↔ 1fr で）伸び縮みさせ、
// 閉じるときは縮み終わってから中身を外す（usePresence）。見た目は styles.css の .m3-collapse。
// 閉じている間は inert にして、残っている中身へフォーカスや読み上げが届かないようにする。
import { useEffect, useState, type ReactNode, type TransitionEvent } from "react";
import { usePresence } from "../hooks";

/** 開閉の見た目だけ（いつ外すかは呼び出し側が usePresence で決める）。表の行のように、外側の要素ごと残したいときに使う。 */
export function CollapseBody({
  open,
  onExited,
  className = "",
  children,
}: {
  open: boolean;
  onExited: () => void;
  className?: string;
  children: ReactNode;
}) {
  // 伸び縮みの間だけ中身を切り取る。開き終わったら外す（フォーカスの枠やツールチップが欠けないように）
  const [settled, setSettled] = useState(false);
  useEffect(() => {
    if (!open) setSettled(false);
  }, [open]);
  const onTransitionEnd = (event: TransitionEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget || event.propertyName !== "grid-template-rows") return;
    if (open) setSettled(true);
    else onExited();
  };
  return (
    <div
      className={`m3-collapse ${open ? "is-open" : ""} ${open && settled ? "is-settled" : ""} ${className}`}
      inert={!open}
      onTransitionEnd={onTransitionEnd}
    >
      <div className="m3-collapse-inner">{children}</div>
    </div>
  );
}

export function Collapse({ open, className, children }: { open: boolean; className?: string; children: ReactNode }) {
  const { present, onExited } = usePresence(open);
  if (!present) return null;
  return (
    <CollapseBody open={open} onExited={onExited} className={className}>
      {children}
    </CollapseBody>
  );
}
