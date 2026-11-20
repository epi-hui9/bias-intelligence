import {
  CATS, JUDGES, LEVELS, MAP, MBYID, MODELS, PNAME, QUESTIONS, RUN, catName, catScore, levelOf, overall, qScore, ranksFor, runScore, tally,
  type Level, type MergeKind, type Question,
} from "../data/leaderboard";
import { ModelLogo } from "../components/ModelLogo";
import { LevelChip, MergeSwitch, Ring, Stack, f2 } from "../components/widgets";

type Props = { id: string; merge: MergeKind; setMerge: (k: MergeKind) => void };

export function ModelPage({ id, merge, setMerge }: Props) {
  const m = MBYID[id];
  if (!m) return <Missing text="That model is missing." back="#/ranking" />;
  const k = merge;
  const rank = ranksFor(k)[id];
  const rv = ranksFor("vote")[id];
  const ra = ranksFor("avg")[id];
  const score = overall(m, k);
  const level = levelOf(score);
  const med = (key: string) => {
    const v = MODELS.map((x) => catScore(x, k, key)).sort((a, b) => a - b);
    return (v[11] + v[12]) / 2;
  };
  const cats = [...CATS].sort((a, b) => catScore(m, k, a.key) - catScore(m, k, b.key));

  const pairs: Record<string, { A?: Question; B?: Question }> = {};
  QUESTIONS.filter((q) => q.type === "paired").forEach((q) => {
    (pairs[q.pairId as string] = pairs[q.pairId as string] || {})[q.variant as "A" | "B"] = q;
  });
  const gaps = Object.entries(pairs)
    .map(([pid, p]) => {
      const a = qScore(m, k, (p.A as Question).questionId);
      const b = qScore(m, k, (p.B as Question).questionId);
      return { pid, p, a, b, gap: Math.abs(a - b) };
    })
    .sort((x, y) => y.gap - x.gap);

  /* vote statistics */
  const allVotes: number[] = [];
  const pj: { l: number; hit: boolean }[][] = [[], [], []];
  const spread: { q: Question; run: number; votes: number[]; v: number }[] = [];
  let blockedN = 0;
  QUESTIONS.forEach((q) =>
    m.qs[q.questionId].runs.forEach((r) => {
      if (r.blocked) { blockedN++; return; }
      const final = levelOf(runScore(r, k) as number);
      r.votes.forEach((l, i) => { allVotes.push(l); pj[Math.floor(i / 3)].push({ l, hit: l === final }); });
      const sc = r.votes.map((l) => MAP[l]);
      const mean = sc.reduce((a, x) => a + x, 0) / 9;
      spread.push({ q, run: r.runIndex, votes: r.votes, v: sc.reduce((a, x) => a + (x - mean) * (x - mean), 0) / 9 });
    })
  );
  spread.sort((a, b) => b.v - a.v);
  const vc = tally(allVotes);
  const agreeAll = Math.round((pj.flat().filter((x) => x.hit).length / allVotes.length) * 100);
  const order = ["N1", "N2", "N3", "C1", "C2", "C3", "P1A", "P1B", "P2A", "P2B"];

  return (
    <>
      <section className="mhero">
        <div className="container">
          <a className="back" href="#/ranking">&larr; Back to ranking</a>
          <div className="mhero-row">
            <div className="mhero-id">
              <ModelLogo provider={m.provider} size={76} />
              <div>
                <span className="section-eyebrow">Rank {rank} of {MODELS.length}</span>
                <h1 className="mhero-name">{m.modelName}</h1>
                <p className="mhero-sub">
                  {PNAME[m.provider]}{m.effort ? <> · <span className="tag">{m.effort} reasoning effort</span></> : null} · {RUN.questionCount} questions, {RUN.runsPerQuestion} runs each
                </p>
              </div>
            </div>
            <div className="mhero-score">
              <Ring value={score} level={level} size={156} />
              <LevelChip level={level} />
            </div>
          </div>
          <div className="grid3">
            <div className="card"><span className="eyebrow">Lowest bias</span><h3>{cats[0].label}</h3><p>{f2(catScore(m, k, cats[0].key))}</p></div>
            <div className="card"><span className="eyebrow">Highest bias</span><h3>{cats[9].label}</h3><p>{f2(catScore(m, k, cats[9].key))}</p></div>
            <div className="card"><span className="eyebrow">Widest paired gap</span><h3>{gaps[0].pid}</h3><p>{gaps[0].p.A?.term} vs {gaps[0].p.B?.term}, gap {f2(gaps[0].gap)}</p></div>
          </div>
        </div>
      </section>

      <div className="container page-body">
        <div className="mergebar">
          <div><span className="eyebrow">Merge rule</span><p>Scores on this page follow the rule you pick.</p></div>
          <MergeSwitch value={merge} onChange={setMerge} />
          <p className="mnote">Majority vote: {f2(overall(m, "vote"))}, rank {rv}. Average score: {f2(overall(m, "avg"))}, rank {ra}.</p>
        </div>

        <section className="sect">
          <h2>Scores by category</h2>
          <p>The dark tick marks the median across all {MODELS.length} models.</p>
          <div className="cats">
            {CATS.map((c) => {
              const s = catScore(m, k, c.key);
              const l = levelOf(s);
              return (
                <div className="cat" key={c.key}>
                  <b>{c.label}</b>
                  <span className="bar"><span className={`f${l}`} style={{ width: `${Math.max(2, s * 100)}%` }} /><i className="tick" style={{ left: `${med(c.key) * 100}%` }} title="Median across all models" /></span>
                  <b className="mono">{f2(s)}</b>
                  <LevelChip level={l} />
                </div>
              );
            })}
          </div>
        </section>

        <section className="sect">
          <h2>How the judges voted</h2>
          <p>{allVotes.length.toLocaleString("en-US")} votes across {RUN.questionCount * RUN.runsPerQuestion - blockedN} scored answers. Each answer gets nine votes, and the merge rule turns them into one label. The final label matched {agreeAll} percent of all votes.</p>
          <Stack votes={allVotes} />
          <div className="legend">
            {([1, 2, 3, 4, 5] as Level[]).map((l) => <span key={l}><LevelChip level={l} plain /> {Math.round((vc[l] / allVotes.length) * 100)} percent</span>)}
          </div>
          <table className="t">
            <thead><tr><th>Judge</th><th>Mean vote score</th><th>Most common label</th><th>Agreed with final label</th></tr></thead>
            <tbody>
              {JUDGES.map((j, ji) => {
                const arr = pj[ji];
                const cc = tally(arr.map((x) => x.l));
                let top = 1;
                for (let l = 2; l <= 5; l++) if (cc[l] > cc[top]) top = l;
                const mean = arr.reduce((a, x) => a + MAP[x.l], 0) / arr.length;
                return (
                  <tr key={j.id}>
                    <td><b>{j.name}</b></td>
                    <td className="mono">{f2(mean)}</td>
                    <td><LevelChip level={top} /></td>
                    <td className="mono">{Math.round((arr.filter((x) => x.hit).length / arr.length) * 100)} percent</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="mute narrow">{blockedN ? `${blockedN} of ${RUN.questionCount * RUN.runsPerQuestion} answers were blocked by a content filter. They carry no score and are left out of every average.` : "Every answer was scored."}</p>
        </section>

        <section className="sect">
          <h2>Where the judges disagreed most</h2>
          <p>These answers drew the widest spread of votes. Open one to read each judge&rsquo;s reason.</p>
          <div className="tbl">
            {spread.slice(0, 5).map(({ q, run, votes }) => (
              <a key={q.questionId + run} className="qrow dis" href={`#/questions/${q.questionId}`}>
                <span className="qid">{q.questionId} <span className="mute">run {run}</span></span>
                <span className="qprompt">{q.prompt}</span>
                <span><Stack votes={votes} /></span>
              </a>
            ))}
          </div>
        </section>

        <section className="sect">
          <h2>Paired questions</h2>
          <p>The same question with one word swapped. A gap of 0 means both versions received equal treatment.</p>
          <div className="tscroll">
            <table className="t">
              <thead><tr><th>Pair</th><th>Category</th><th>Swapped terms</th><th>Score A / B</th><th>Gap</th></tr></thead>
              <tbody>
                {gaps.map((g) => (
                  <tr key={g.pid}>
                    <td className="mono"><b>{g.pid}</b></td>
                    <td>{catName((g.p.A as Question).category)}</td>
                    <td>{g.p.A?.term} <span className="mute">vs</span> {g.p.B?.term}</td>
                    <td className="mono">{f2(g.a)} / {f2(g.b)}</td>
                    <td className="gapcell">
                      <div className="gapwrap">
                        <span className="bar"><span className={`f${g.gap > 0.2 ? 4 : g.gap > 0.1 ? 3 : 2}`} style={{ width: `${Math.min(100, g.gap * 250)}%` }} /></span>
                        <b className="mono">{f2(g.gap)}</b>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="sect">
          <h2>Every question at a glance</h2>
          <p>Each square is one question. Select a square to read the answers.</p>
          <div className="qgrid">
            <div />
            {order.map((o, i) => <div key={i} className="lab center"><span className="full">{o[0] === "N" ? "Neutral" : o[0] === "C" ? "Charged" : "Paired"}</span><span className="abbr">{o[0]}</span></div>)}
            {CATS.map((c) => (
              <FragmentCat key={c.key} label={catName(c.key)}>
                {order.map((o) => {
                  const qid = `${c.code}-${o}`;
                  const s = qScore(m, k, qid);
                  const l = levelOf(s);
                  return <a key={qid} className={`l${l}`} href={`#/questions/${qid}`} title={`${qid}: ${f2(s)}, ${LEVELS[l]}`} aria-label={qid} />;
                })}
              </FragmentCat>
            ))}
          </div>
          <div className="legend">{([1, 2, 3, 4, 5] as Level[]).map((l) => <LevelChip key={l} level={l} plain />)}</div>
        </section>
      </div>
    </>
  );
}

function FragmentCat({ label, children }: { label: string; children: React.ReactNode }) {
  return (<><div className="lab">{label}</div>{children}</>);
}

export function Missing({ text, back }: { text: string; back: string }) {
  return (
    <section className="section"><div className="container"><h1 className="section-heading">{text}</h1><p className="section-lede"><a className="link" href={back}>Go back</a></p></div></section>
  );
}
