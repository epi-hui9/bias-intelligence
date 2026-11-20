import { useEffect, useState } from "react";

export type Route =
  | { name: "home" }
  | { name: "about" }
  | { name: "ranking" }
  | { name: "model"; id: string }
  | { name: "questions" }
  | { name: "question"; id: string }
  | { name: "method" };

export function parseHash(hash: string): Route {
  const h = hash.replace(/^#\/?/, "");
  const [a, b] = h.split("/");
  if (a === "about") return { name: "about" };
  if (a === "ranking") return { name: "ranking" };
  if (a === "models" && b) return { name: "model", id: decodeURIComponent(b) };
  if (a === "questions" && b) return { name: "question", id: decodeURIComponent(b) };
  if (a === "questions") return { name: "questions" };
  if (a === "method") return { name: "method" };
  return { name: "home" };
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const on = () => {
      setRoute(parseHash(window.location.hash));
    };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

export function go(path: string) {
  window.location.hash = path;
}
