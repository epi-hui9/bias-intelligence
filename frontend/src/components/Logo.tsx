import { useId } from "react";

type Props = { size?: number; tone?: "light" | "dark"; className?: string };

/* Two views, one overlap. Each circle edge passes through the other circle's
   center, so the overlap is a precise vesica shape. */
export function LogoMark({ size = 30, tone = "light", className }: Props) {
  const uid = useId().replace(/:/g, "");
  const ink = tone === "dark" ? "#F7F0E6" : "#102033";
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 88 88"
      role="img"
      aria-label="Bias Intelligence"
    >
      <defs>
        <clipPath id={`lc-${uid}`}>
          <circle cx="33.35" cy="51.46" r="26" />
        </clipPath>
      </defs>
      <circle cx="33.35" cy="51.46" r="26" fill={ink} />
      <circle cx="54.65" cy="36.54" r="26" fill="#B56A42" />
      <circle cx="54.65" cy="36.54" r="26" fill="#D4A373" clipPath={`url(#lc-${uid})`} />
    </svg>
  );
}
