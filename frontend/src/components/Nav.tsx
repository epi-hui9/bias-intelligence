import type { Route } from "../lib/router";

type Props = {
  route: Route["name"];
  hasEvidence: boolean;
  onOpenAbout: () => void;
  onOpenHome: (target?: string) => void;
};

const LINKS: { name: Route["name"][]; href: string; label: string }[] = [
  { name: ["ranking", "model"], href: "#/ranking", label: "Ranking" },
  { name: ["questions", "question"], href: "#/questions", label: "Questions" },
  { name: ["method"], href: "#/method", label: "Method" },
];

export function Nav({ route, hasEvidence, onOpenAbout, onOpenHome }: Props) {
  return (
    <header className="nav">
      <div className="container nav-row">
        <a className="brand" href="#top" onClick={() => onOpenHome("top")}>
          <span className="brand-mark" aria-hidden />
          <span>Bias Intelligence</span>
        </a>

        <nav className="nav-links" aria-label="Primary">
          {LINKS.map((l) => (
            <a key={l.href} href={l.href} aria-current={l.name.includes(route) ? "page" : undefined}>
              {l.label}
            </a>
          ))}
          <a className="nav-cta" href="#workspace" aria-current={route === "home" ? "page" : undefined} onClick={() => onOpenHome("workspace")}>
            Writing tool
          </a>
          {route === "home" && hasEvidence && <a className="nav-extra" href="#social-evidence">Evidence</a>}
          <a href="#about" aria-current={route === "about" ? "page" : undefined} onClick={onOpenAbout}>
            About
          </a>
        </nav>
      </div>
    </header>
  );
}
