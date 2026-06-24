// Pure client-rendered SPA: no SSR, no prerendering. FastAPI serves the static
// fallback for every route and the Svelte client router takes over.
export const ssr = false;
export const prerender = false;
