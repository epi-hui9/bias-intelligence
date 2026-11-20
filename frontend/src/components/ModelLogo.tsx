import { logoSvg, providerOf } from "../lib/brands";
import type { ProviderKey } from "../data/leaderboard";

type Props = {
  provider?: ProviderKey | null;
  name?: string;
  size?: number;
  tone?: "light" | "dark";
};

/* Provider mark on a rounded tile. Pass a provider key, or a model name to infer it. */
export function ModelLogo({ provider, name, size = 36, tone = "light" }: Props) {
  const key = provider ?? (name ? providerOf(name) : null);
  return (
    <span
      className={`mlogo mlogo-${tone}`}
      style={{ width: size, height: size, borderRadius: Math.round(size * 0.3) }}
      aria-hidden
    >
      {key ? (
        <span className="mlogo-glyph" style={{ width: size * 0.56, height: size * 0.56 }} dangerouslySetInnerHTML={{ __html: logoSvg(key) }} />
      ) : (
        <span className="mlogo-fallback">{(name ?? "?").slice(0, 1)}</span>
      )}
    </span>
  );
}
