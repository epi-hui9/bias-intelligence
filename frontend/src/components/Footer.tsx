import { LogoMark } from "./Logo";
export function Footer() {
  return (
    <footer className="footer">
      <div className="container footer-row">
        <div className="brand">
          <LogoMark size={38} className="brand-logo" />
          <span>Bias Intelligence</span>
        </div>
        <div className="footer-meta">Humanity and Technoscience Lab · Northwestern University</div>
      </div>
    </footer>
  );
}
