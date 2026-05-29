import { useEffect, useState } from "react";

// Small fetch hook for the precomputed JSON files under /data. Returns { data, error,
// loading } so pages can render loading / error / content states consistently.
export default function useJson(path) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    setData(null);
    setError(null);
    fetch(path)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, [path]);

  return { data, error, loading: !data && !error };
}
