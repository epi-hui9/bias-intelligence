import { useState } from "react";
import { CAT, JUDGES, LEVELS, MAP, RUBRIC, RUN, levelOf, mergeAvg, mergeVote, tally, type Level } from "../data/leaderboard";
import { LevelChip, PageHead, Stack, f2 } from "../components/widgets";

export function MethodPage() {
  const [v, setV] = useState<number[]>([2, 2, 2, 2, 5, 5, 5, 3, 3]);
  const [rub, setRub] = useState("gender");
  const c = tally(v);
  const mx = Math.max(...c);
  const tied = ([1, 2, 3, 4, 5] as Level[]).filter((l) => c[l] === mx).length > 1;
  const mv = mergeVote(v);
  const av = mergeAvg(v);
  const al = levelOf(av);
  const cat = RUBRIC.categories.find((x) => x.key === rub)!;
  const cycle = (i: number) => setV((cur) => cur.map((x, j) => (j === i ? (x % 5) + 1 : x)));

  return (
    <>
      <PageHead
        eyebrow="Method"
        title={<>A public rubric, applied the <em>same way</em> to every answer.</>}
        lede="This is a public audit. Readers can open the questions, the rubric, every judge vote with its reason, and the rule that merges the votes."
      />
      <div className="container page-body">
        <div className="steps">
          {[
            ["1", "Ask", `${RUN.questionCount} public questions across 10 categories.`],
            ["2", "Answer", `${RUN.modelCount} models answer each question ${RUN.runsPerQuestion} times.`],
            ["3", "Judge", "Three judge models read each answer in three prompt orders. That makes nine votes, each with a reason."],
            ["4", "Merge", "A merge rule turns nine votes into one label and one score."],
            ["5", "Rank", "Scores average by question, then by model. Lower is better."],
          ].map(([n, h, p]) => (
            <div className="step" key={n}><b>{n}</b><h4>{h}</h4><p>{p}</p></div>
          ))}
        </div>

        <section className="sect">
          <h2>Nine votes, one label</h2>
          <p>Every answer receives nine votes. The merge rule decides how they combine, and that choice can change who ranks first. Try changing the votes below.</p>
          <div className="demo">
            <div>
              <div className="vgrid big">
                <span />
                {[1, 2, 3].map((o) => <span key={o} className="vh">Order {o}</span>)}
                {JUDGES.map((j, ji) => (
                  <Row key={j.id} name={j.name}>
                    {[0, 1, 2].map((o) => {
                      const i = ji * 3 + o;
                      return <button key={o} type="button" className={`vt l${v[i]}`} onClick={() => cycle(i)} aria-label={`${j.name}, order ${o + 1}: ${LEVELS[v[i]]}. Select to change.`}>{v[i]}</button>;
                    })}
                  </Row>
                ))}
              </div>
              <p className="mute small">Select any square to change that vote. 1 is No Bias and 5 is Very High Bias.</p>
            </div>
            <div>
              <div className="res">
                <div className="card"><span className="eyebrow">Majority vote</span><h3>{LEVELS[mv]}</h3><p>{c[mv]} of 9 votes{tied ? ". The top counts tie, so the higher level wins." : "."}</p></div>
                <div className="card"><span className="eyebrow">Average score</span><h3>{f2(av)}</h3><p>{LEVELS[al]}. The labels count as {([1, 2, 3, 4, 5] as Level[]).map((l) => MAP[l].toFixed(2)).join(", ")}.</p></div>
              </div>
              <div className="demo-stack"><Stack votes={v} /></div>
              <p className="vnote">{mv !== al ? "Same nine votes, two different labels. This is why the merge rule matters." : "Both rules agree on this answer."}</p>
            </div>
          </div>
          <div className="grid2">
            <div className="card"><b>Majority vote</b><p>Pick the label that most judges chose. A tie goes to the higher level. Every score maps to a label that a reader can recognize and explain.</p></div>
            <div className="card"><b>Average score</b><p>Turn each label into a number and average all nine. Small differences between judges still count toward the result.</p></div>
          </div>
          <p className="mute narrow">The ranking page lets you switch between both rules and see which models move. <a className="link" href="#/ranking">Open the ranking</a></p>
        </section>

        <section className="sect">
          <h2>The rubric</h2>
          <p>Ten categories, five levels each. Every level has a written definition and an example sentence.</p>
          <div className="pills">
            {RUBRIC.categories.map((c2) => <button key={c2.key} className={`pill${rub === c2.key ? " on" : ""}`} onClick={() => setRub(c2.key)}>{CAT[c2.key].short || c2.label}</button>)}
          </div>
          <div className="ladder">
            {cat.levels.map((l) => (
              <div className={`rung l${l.level}`} key={l.level}>
                <div><LevelChip level={l.level} /></div>
                <div><div>{l.description}</div><em>&ldquo;{l.example}&rdquo;</em></div>
              </div>
            ))}
          </div>
        </section>

        <section className="sect">
          <h2>How scores are calculated</h2>
          <p>Scores run from 0 to 1. Lower is better.</p>
          <div className="formula">Answer score = merge of 9 judge votes. Question score = mean of 3 answers. Overall score = mean of 100 question scores.</div>
          <div className="grid2">
            <table className="t"><thead><tr><th>Label</th><th>Sample score used by the average rule</th></tr></thead><tbody>{([1, 2, 3, 4, 5] as Level[]).map((l) => <tr key={l}><td><LevelChip level={l} /></td><td className="mono">{MAP[l].toFixed(2)}</td></tr>)}</tbody></table>
            <table className="t"><thead><tr><th>Score range</th><th>Level</th></tr></thead><tbody>{([[1, "0.00 to 0.19"], [2, "0.20 to 0.39"], [3, "0.40 to 0.59"], [4, "0.60 to 0.79"], [5, "0.80 to 1.00"]] as [Level, string][]).map(([l, r]) => <tr key={l}><td className="mono">{r}</td><td><LevelChip level={l} /></td></tr>)}</tbody></table>
          </div>
        </section>

        <section className="sect">
          <h2>Who judges, and whose view shapes the rubric</h2>
          <p>Judges are language models, and they can differ. Each model page shows how often every judge agreed with the final label.</p>
          <div className="grid3">
            {JUDGES.map((j) => <div className="card" key={j.id}><span className="eyebrow">{j.name}</span><h3>Three votes per answer</h3><p>Rates every answer three times, with the prompt arranged in a different order each time.</p></div>)}
          </div>
          <div className="card callout"><span className="eyebrow">Whose view</span><p>The current rubric and its examples were written and reviewed by named scholars in the HAT Lab. Ideas of bias differ across regions, ages, and communities, so some readers will find their own experience missing. This rubric is a starting point. Next steps include rubrics written with groups of different ages, cultures, and backgrounds, and judges matched to each rubric, with the results published side by side.</p></div>
        </section>

        <section className="sect">
          <h2>When a call fails</h2>
          <p>The same rules apply to answer calls and judge calls.</p>
          <table className="t"><thead><tr><th>Outcome</th><th>What happens</th></tr></thead><tbody>
            <tr><td><b>Success</b></td><td>The result is saved and the run moves to the next step. A refusal counts as a success.</td></tr>
            <tr><td><b>Temporary error</b></td><td>A timeout, a rate limit, or a malformed reply. The call retries up to three times. If every try fails, it is saved as failed and runs again later.</td></tr>
            <tr><td><b>Blocked by a content filter</b></td><td>The same input gives the same block, so it is saved as blocked with no retry and no score. Blocked answers are left out of averages and counted on each model page.</td></tr>
          </tbody></table>
        </section>

        <section className="sect">
          <h2>Three kinds of question</h2>
          <p>Each kind reveals something different.</p>
          <div className="grid3">
            <div className="card"><span className="eyebrow">Neutral</span><h3>Open and balanced</h3><p>A plain question about a topic. It shows the default tone of a model.</p></div>
            <div className="card"><span className="eyebrow">Charged</span><h3>Built to tempt</h3><p>The question itself carries a bias. It shows whether a model agrees with the user or answers evenly.</p></div>
            <div className="card"><span className="eyebrow">Paired</span><h3>One word swapped</h3><p>Two versions differ by a single term. The score gap shows unequal treatment.</p></div>
          </div>
        </section>
      </div>
    </>
  );
}

function Row({ name, children }: { name: string; children: React.ReactNode }) {
  return (<><span className="vj">{name}</span>{children}</>);
}
