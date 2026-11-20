import { LOGO_SVG } from "./logoSvg";
import type { ProviderKey } from "../data/leaderboard";

export function logoSvg(provider: ProviderKey): string {
  return LOGO_SVG[provider] ?? "";
}

/* Map a model display name (writing tool or leaderboard) to its provider. */
export function providerOf(modelName: string): ProviderKey | null {
  const n = modelName.toLowerCase();
  if (n.startsWith("gpt")) return "openai";
  if (n.startsWith("claude")) return "anthropic";
  if (n.startsWith("llama")) return "meta";
  if (n.startsWith("deepseek")) return "deepseek";
  if (n.startsWith("grok")) return "xai";
  if (n.startsWith("mistral")) return "mistral";
  if (n.startsWith("phi")) return "microsoft";
  if (n.startsWith("kimi")) return "moonshot";
  if (n.startsWith("qwen")) return "alibaba";
  if (n.startsWith("command")) return "cohere";
  return null;
}
