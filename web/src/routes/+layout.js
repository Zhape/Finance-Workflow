// Applies to every route. The app is a client-rendered shell over the worker
// API: there is no data to render on a server, and prerendering would bake in
// an empty, signed-out page.
export const ssr = false;
export const prerender = false;
