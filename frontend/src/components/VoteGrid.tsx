import { useState } from "react";
import { JUDGES, LEVELS, levelOf, tally, reasonFor, type Question, type Run } from "../data/leaderboard";
import { LevelChip, Stack, f2 } from "./widgets";

type Props = { modelId: string; q: Question; run: Run };

/* Nine votes for one answer: 3 judges x 3 prompt orders. Select a vote to read its reason. */
export function VoteGrid({ modelId, q, run }: Props) {
  const [sel, setSel] = useState<number | null>(null);
  if (run.blocked) return null;
  const c = tally(run.votes);
  return (
    <div className="votes">
      <div className="vgrid">
        <span />
        {[1, 2, 3].map((o) => <span key={o} className="vh">Order {o}</span>)}
        {JUDGES.map((j, ji) => (
          <FragmentRow key={j.id} name={j.name}>
            {[0, 1, 2].map((o) => {
              const vi = ji * 3 + o;
              const l = run.votes[vi];
              return (
                <button key={o} type="button" className={`vt l${l}${sel === vi ? " sel" : ""}`} onClick={() => setSel(vi)} aria-label={`${j.name}, order ${o + 1}: ${LEVELS[l]}`} title={`${j.name}, order ${o + 1}: ${LEVELS[l]}`}>
                  {l}
                </button>
              );
            })}
          </FragmentRow>
        ))}
      </div>
      <div className="vside">
        <Stack votes={run.votes} />
        <p className="vnote">Majority: <b>{LEVELS[run.mv as number]}</b>, {c[run.mv as number]} of 9 votes. Average: <b>{f2(run.avg as number)}</b>, {LEVELS[levelOf(run.avg as number)]}.</p>
        <div className="vreason">
          {sel === null ? (
            <span className="mute">Select a vote to read the judge&rsquo;s reason.</span>
          ) : (
            <>
              <b>{JUDGES[Math.floor(sel / 3)].name}, order {(sel % 3) + 1}</b> chose <LevelChip level={run.votes[sel]} />
              <br />
              {reasonFor(modelId, q, run.runIndex, sel, run.votes[sel])}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function FragmentRow({ name, children }: { name: string; children: React.ReactNode }) {
  return (
    <>
      <span className="vj">{name}</span>
      {children}
    </>
  );
}

