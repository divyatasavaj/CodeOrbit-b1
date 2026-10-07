// API base URL resolution.
//   * VITE_API_URL (if set) always wins - use it to point at any backend.
//   * In local dev (`npm run dev`) default to the local FastAPI backend.
//   * In production builds keep the deployed backend.
export const API =
    import.meta.env.VITE_API_URL ||
    (import.meta.env.DEV ? "http://localhost:8000" : "https://codeorbit-wi1m.onrender.com");
