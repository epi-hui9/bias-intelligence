import { LEVELS, MERGE_LABEL, tally, type Level, type MergeKind } from "../data/leaderboard";

export const f2 = (n: number) => n.toFixed(2);

export function LevelChip({ level, plain }: { level: number; plain?: boolean }) {
  return (
    <span className={`lv l${level}${plain ? " plain" : ""}`}>
      <i />
      {LEVELS[level]}
    </span>
  );
}

export function MergeSwitch({ value, onChange }: { value: MergeKind; onChange: (k: MergeKind) => void }) {
  return (
    <div className="seg" role="group" aria-label="Merge rule">
      {(["vote", "avg"] as MergeKind[]).map((k) => (
        <button key={k} type="button" className={value === k ? "on" : ""} onClick={() => onChange(k)}>
          {MERGE_LABEL[k]}
        </button>
      ))}
    </div>
  );
}

/* Stacked bar of how many votes each level got. */
export function Stack({ votes }: { votes: number[] }) {
  const c = tally(votes);
  return (
    <div className="stack" role="img" aria-label="Votes by level">
      {([1, 2, 3, 4, 5] as Level[]).filter((l) => c[l]).map((l) => (
        <span key={l} className={`l${l}`} style={{ flex: c[l] }} title={`${LEVELS[l]}: ${c[l]}`}>
          {c[l]}
        </span>
      ))}
    </div>
  );
}

/* Gauge ring for a 0..1 score. */
export function Ring({ value, size = 150, level }: { value: number; size?: number; level: number }) {
  const r = (size - 14) / 2;
  const c = 2 * Math.PI * r;
  return (
    <svg className={`ring l${level}`} width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={`Score ${f2(value)}`}>
      <circle cx={size / 2} cy={size / 2} r={r} className="ring-track" />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        className="ring-fill"
        strokeDasharray={`${c * Math.min(1, Math.max(0.02, value))} ${c}`}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
      />
      <text x="50%" y="50%" textAnchor="middle" dominantBaseline="central" className="ring-num">
        {f2(value)}
      </text>
    </svg>
  );
}

export function PageHead({ eyebrow, title, lede, children }: { eyebrow: string; title: React.ReactNode; lede?: React.ReactNode; children?: React.ReactNode }) {
  return (
    <header className="phead">
      <div className="container">
        <span className="section-eyebrow">{eyebrow}</span>
        <h1 className="phead-title">{title}</h1>
        {lede && <p className="phead-lede">{lede}</p>}
        {children}
      </div>
    </header>
  );
}
