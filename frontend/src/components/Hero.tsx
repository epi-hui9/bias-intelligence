import { ArrowRight } from "./Icons";
import { ModelLogo } from "./ModelLogo";
import { PROVIDERS } from "../data/leaderboard";

type Props = {
  onAnalyze: () => void;
};

export function Hero({ onAnalyze }: Props) {
  return (
    <section className="hero" id="top">
      <div className="hero-glow" aria-hidden />
      <div className="container hero-grid">
        <div>
          <span className="section-eyebrow">Humanity and Technoscience Lab</span>
          <h1>
            Neutral words can leave <em>our assumptions untouched.</em>
          </h1>

          <p className="hero-sub">
            A model can rewrite a sentence without making its writer less biased. Bias
            Intelligence connects language to decades of social research so we can
            understand difference and carry that understanding beyond the screen.
          </p>

          <div className="hero-cta">
            <button className="btn btn-accent btn-lg" onClick={onAnalyze}>
              Start analysis <ArrowRight size={16} />
            </button>
            <a className="btn btn-ghost btn-lg" href="#/ranking">See the model ranking</a>
          </div>

          <div className="hero-logos" aria-label="Providers compared">
            {PROVIDERS.map((p) => (
              <span key={p.key} title={p.name}><ModelLogo provider={p.key} size={34} /></span>
            ))}
          </div>
        </div>

        <div className="preview">
          <div className="preview-head">
            <span className="preview-label">A signal worth examining</span>
            <span className="preview-score">High · 0.78</span>
          </div>

          <p className="preview-quote">
            “The candidate seemed <span className="hl">surprisingly articulate</span>.”
          </p>

          <div className="preview-insight">
            <span>Why it matters</span>
            <p>“Surprisingly” reveals an expectation gap: articulate speech was unexpected for this person.</p>
          </div>
          <div className="preview-reflection">
            <span>Reflect</span>
            <p>What made clear communication feel surprising here?</p>
          </div>
        </div>
      </div>
    </section>
  );
}
