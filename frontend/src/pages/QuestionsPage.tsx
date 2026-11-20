import { useState } from "react";
import { CATS, QBYID, MODELS, MBYID, PNAME, QUESTIONS, TYPE_LABEL, CAT, catName, displayName, genAnswer, levelOf, qScore, runLevel, runScore, type MergeKind, type QType } from "../data/leaderboard";
import { ModelLogo } from "../components/ModelLogo";
import { VoteGrid } from "../components/VoteGrid";
import { LevelChip, MergeSwitch, PageHead, f2 } from "../components/widgets";
import { Missing } from "./ModelPage";

export function QuestionsPage() {
  const [cat, setCat] = useState("all");
  const [type, setType] = useState<"all" | QType>("all");
  const [text, setText] = useState("");
  const t = text.trim().toLowerCase();
  const list = QUESTIONS.filter((q) => (cat === "all" || q.category === cat) && (type === "all" || q.type === type) && (!t || q.prompt.toLowerCase().includes(t) || q.questionId.toLowerCase().includes(t)));
  return (
    <>
      <PageHead
        eyebrow="Question set"
        title={<>Every question is <em>public.</em></>}
        lede="Each category has three neutral questions, three charged questions, and two pairs that swap a single word. Select a question to read what each model answered."
      />
      <div className="container page-body">
        <div className="pills">
          {[["all", "All categories"], ...CATS.map((c) => [c.key, catName(c.key)])].map(([k, l]) => (
            <button key={k} className={`pill${cat === k ? " on" : ""}`} onClick={() => setCat(k)}>{l}</button>
          ))}
        </div>
        <div className="controls">
          <div className="pills tight">
            {(["all", "neutral", "charged", "paired"] as const).map((k) => (
              <button key={k} className={`pill${type === k ? " on" : ""}`} onClick={() => setType(k)}>{k === "all" ? "All types" : TYPE_LABEL[k]}</button>
            ))}
          </div>
          <label className="search"><span>Search</span><input value={text} onChange={(e) => setText(e.target.value)} placeholder="Words or ID" autoComplete="off" /></label>
        </div>
        <p className="mute count">{list.length} of {QUESTIONS.length} questions</p>
        <div className="tbl">
          {list.map((q) => (
            <a key={q.questionId} className="qrow" href={`#/questions/${q.questionId}`}>
              <span className="qid">{q.questionId}</span>
              <span className="qcat mute">{catName(q.category)}</span>
              <span className="qtype"><span className={`tag ty-${q.type}`}>{TYPE_LABEL[q.type]}</span></span>
              <span className="qprompt">{q.prompt}</span>
            </a>
          ))}
          {!list.length && <div className="empty"><h3>No questions match</h3><p>Clear a filter to see more.</p></div>}
        </div>
      </div>
    </>
  );
}

export function QuestionPage({ id, merge, setMerge }: { id: string; merge: MergeKind; setMerge: (k: MergeKind) => void }) {
  const q = QBYID[id];
  const [open, setOpen] = useState<string | null>(null);
  if (!q) return <Missing text="That question is missing." back="#/questions" />;
  const k = merge;
  const res = MODELS.map((m) => ({ m, s: qScore(m, k, id) })).sort((a, b) => a.s - b.s);
  const mate = q.pairId ? QBYID[q.pairId + (q.variant === "A" ? "B" : "A")] : null;
  const blockedN = MODELS.reduce((a, m) => a + m.qs[id].runs.filter((x) => x.blocked).length, 0);
  return (
    <>
      <section className="mhero qhero">
        <div className="container">
          <a className="back" href="#/questions">&larr; Back to questions</a>
          <div className="qhead-tags">
            <span className="eyebrow">{q.questionId}</span>
            <span className={`tag ty-${q.type}`}>{TYPE_LABEL[q.type]}</span>
            <span className="tag">{CAT[q.category].label}</span>
          </div>
          <h1 className="qhead-title">{q.prompt}</h1>
          {mate && <p className="mute">Paired with <a className="link" href={`#/questions/${mate.questionId}`}>{mate.questionId}</a>, which uses &ldquo;{mate.term}&rdquo;.</p>}
        </div>
      </section>
      <div className="container page-body">
        <section className="sect first">
          <h2>Answers from {MODELS.length} models</h2>
          <p>Sorted from lowest to highest bias. Each small bar in a row is one judge vote, in groups of nine for the three answers. Open a row to read the answers and every vote.</p>
          <div className="mergebar slim">
            <div><span className="eyebrow">Merge rule</span><p>Labels and scores below follow the rule you pick.</p></div>
            <MergeSwitch value={merge} onChange={setMerge} />
            <p className="mnote">{blockedN ? `${blockedN} blocked ${blockedN === 1 ? "answer has" : "answers have"} no score.` : ""}</p>
          </div>
          {res.map(({ m, s }, i) => {
            const isOpen = open === m.modelId;
            return (
              <div className={`ans${isOpen ? " open" : ""}`} key={m.modelId}>
                <button type="button" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : m.modelId)}>
                  <span className="mono mute">{i + 1}</span>
                  <span className="ans-id"><ModelLogo provider={m.provider} size={34} /><span><b>{displayName(m)}</b><small>{PNAME[m.provider]}</small></span></span>
                  <span className="strip" aria-hidden>
                    {MBYID[m.modelId].qs[id].runs.map((r) => (
                      <span className="grp" key={r.runIndex}>{r.blocked ? Array.from({ length: 9 }, (_, z) => <i key={z} className="blk" />) : r.votes.map((l, z) => <i key={z} className={`l${l}`} />)}</span>
                    ))}
                  </span>
                  <span className="ans-score"><b className="mono">{f2(s)}</b><LevelChip level={levelOf(s)} /></span>
                  <span className="chev" aria-hidden>&#9662;</span>
                </button>
                {isOpen && (
                  <div className="body">
                    {m.qs[id].runs.map((run) => {
                      const rs = runScore(run, k);
                      const rl = runLevel(run, k);
                      return run.blocked ? (
                        <div className="run" key={run.runIndex}>
                          <span className="eyebrow">Answer {run.runIndex}</span>
                          <p className="mute">A content filter blocked this call. The same input gives the same block, so it was saved as blocked with no retry. It carries no score and is left out of averages.</p>
                          <div className="r"><span className="tag">Blocked</span></div>
                        </div>
                      ) : (
                        <div className="run" key={run.runIndex}>
                          <span className="eyebrow">Answer {run.runIndex}</span>
                          <div>
                            <p>{genAnswer(m.modelId, q, run, run.baseLevel)}</p>
                            <VoteGrid modelId={m.modelId} q={q} run={run} />
                          </div>
                          <div className="r"><b className="mono">{f2(rs as number)}</b><div><LevelChip level={rl as number} /></div></div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </section>
      </div>
    </>
  );
}
