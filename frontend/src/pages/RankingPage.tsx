import { useMemo, useState } from "react";
import {
  CATS, MODELS, PNAME, PROVIDERS, QUESTIONS, RUN, catName, displayName, levelOf, overall, catScore, ranksFor,
  type MergeKind, type Model,
} from "../data/leaderboard";
import { ModelLogo } from "../components/ModelLogo";
import { LevelChip, MergeSwitch, f2 } from "../components/widgets";

type Props = { merge: MergeKind; setMerge: (k: MergeKind) => void };

export function RankingPage({ merge, setMerge }: Props) {
  const [cat, setCat] = useState<string>("overall");
  const [query, setQuery] = useState("");
  const other: MergeKind = merge === "vote" ? "avg" : "vote";

  const val = (m: Model) => (cat === "overall" ? overall(m, merge) : catScore(m, merge, cat));
  const sorted = useMemo(() => [...MODELS].sort((a, b) => val(a) - val(b)), [merge, cat]); // eslint-disable-line react-hooks/exhaustive-deps
  const rank = ranksFor(merge, cat);
  const rankOther = ranksFor(other, cat);
  const median = useMemo(() => {
    const v = MODELS.map(val).sort((a, b) => a - b);
    return (v[11] + v[12]) / 2;
  }, [merge, cat]); // eslint-disable-line react-hooks/exhaustive-deps
  const q = query.trim().toLowerCase();
  const rows = sorted.filter((m) => !q || displayName(m).toLowerCase().includes(q) || PNAME[m.provider].toLowerCase().includes(q));
  const podium = [sorted[1], sorted[0], sorted[2]];

  const byProvider = PROVIDERS.map((p) => {
    const ms = MODELS.filter((m) => m.provider === p.key);
    const mean = ms.reduce((a, m) => a + val(m), 0) / ms.length;
    return { p, n: ms.length, mean };
  }).sort((a, b) => a.mean - b.mean);

  return (
    <>
      <section className="stage">
        <div className="stage-glow" aria-hidden />
        <div className="container stage-inner">
          <span className="section-eyebrow on-dark">Public ranking · {new Date(RUN.runDate).toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" })}</span>
          <h1 className="stage-title">
            Which models treat <em>everyone</em> evenly?
          </h1>
          <p className="stage-lede">
            {RUN.modelCount} language models answered the same {RUN.questionCount} public questions. Three judge models read every answer and cast nine votes, and a published rule turns the votes into one score. Lower is better.
          </p>
          <div className="stage-stats">
            <div><b>{RUN.modelCount}</b><span>models</span></div>
            <div><b>{RUN.questionCount}</b><span>questions</span></div>
            <div><b>{CATS.length}</b><span>categories</span></div>
            <div><b>{RUN.votesPerAnswer}</b><span>votes per answer</span></div>
          </div>

          <div className="podium" role="list" aria-label="Top three models">
            {podium.map((m, i) => {
              const place = [2, 1, 3][i];
              return (
                <a key={m.modelId} role="listitem" className={`pod pod-${place}`} href={`#/models/${m.modelId}`}>
                  <span className="pod-rank">{place}</span>
                  <ModelLogo provider={m.provider} size={place === 1 ? 64 : 52} tone="dark" />
                  <span className="pod-name">{displayName(m)}</span>
                  <span className="pod-prov">{PNAME[m.provider]}</span>
                  <span className="pod-score">{f2(val(m))}</span>
                  <span className={`pod-level l${levelOf(val(m))}`}>{["", "No Bias", "Low Bias", "Medium Bias", "High Bias", "Very High Bias"][levelOf(val(m))]}</span>
                </a>
              );
            })}
          </div>
        </div>
      </section>

      <section className="marquee" aria-label="Providers in this ranking">
        <div className="marquee-track">
          {[...PROVIDERS, ...PROVIDERS].map((p, i) => (
            <span className="marquee-item" key={p.key + i} aria-hidden={i >= PROVIDERS.length}>
              <ModelLogo provider={p.key} size={30} />
              {p.name}
            </span>
          ))}
        </div>
      </section>

      <section className="section tight">
        <div className="container">
          <div className="controls">
            <div className="controls-left">
              <MergeSwitch value={merge} onChange={setMerge} />
              <p className="mnote">Scores follow the rule you pick. The arrows show how a model would move under the other rule.</p>
            </div>
            <label className="search">
              <span>Find</span>
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Model or provider" autoComplete="off" />
            </label>
          </div>

          <div className="pills" role="tablist" aria-label="Category">
            {[["overall", "Overall"], ...CATS.map((c) => [c.key, catName(c.key)])].map(([k, l]) => (
              <button key={k} className={`pill${cat === k ? " on" : ""}`} onClick={() => setCat(k)}>{l}</button>
            ))}
          </div>

          <div className="rank-table" role="table" aria-label="Model ranking">
            <div className="rank-head" role="row">
              <span>Rank</span><span>Model</span><span>Score, with the median marked</span><span className="r">Score</span><span>Level</span>
            </div>
            {rows.map((m) => {
              const s = val(m);
              const shift = rankOther[m.modelId] - rank[m.modelId];
              const l = levelOf(s);
              return (
                <a key={m.modelId} className={`rank-row l${l}`} href={`#/models/${m.modelId}`} role="row">
                  <span className="rk">
                    <b>{rank[m.modelId]}</b>
                    {shift !== 0 && (
                      <i className={`shift ${shift > 0 ? "up" : "down"}`} title={`Rank ${rankOther[m.modelId]} under ${other === "vote" ? "majority vote" : "average score"}`}>
                        {shift > 0 ? "▲" : "▼"}{Math.abs(shift)}
                      </i>
                    )}
                  </span>
                  <span className="rm">
                    <ModelLogo provider={m.provider} size={38} />
                    <span>
                      <b>{displayName(m)}</b>
                      <small>{PNAME[m.provider]}</small>
                    </span>
                  </span>
                  <span className="rbar">
                    <span className="bar"><span style={{ width: `${Math.max(2, s * 100)}%` }} /><i className="tick" style={{ left: `${median * 100}%` }} title="Median" /></span>
                  </span>
                  <span className="rs r mono">{f2(s)}</span>
                  <span className="rl"><LevelChip level={l} /></span>
                </a>
              );
            })}
            {!rows.length && <div className="empty"><h3>No model matches</h3><p>Clear the search to see all {MODELS.length} models.</p></div>}
          </div>
        </div>
      </section>

      <section className="section">
        <div className="container">
          <h2 className="section-heading sm">By provider</h2>
          <p className="section-lede sm">The mean score of every model from each provider, under the rule and category you picked above.</p>
          <div className="provider-grid">
            {byProvider.map(({ p, n, mean }, i) => (
              <div className="pcard" key={p.key}>
                <ModelLogo provider={p.key} size={44} />
                <div>
                  <b>{p.name}</b>
                  <small>{n} {n === 1 ? "model" : "models"} · place {i + 1}</small>
                </div>
                <span className="pscore mono">{f2(mean)}</span>
                <span className="bar"><span className={`f${levelOf(mean)}`} style={{ width: `${Math.max(3, mean * 100)}%` }} /></span>
              </div>
            ))}
          </div>
          <p className="footnote">Sample data for layout review. Run {RUN.runId}, rubric {RUN.rubricVersion}, {QUESTIONS.length} questions.</p>
        </div>
      </section>
    </>
  );
}
