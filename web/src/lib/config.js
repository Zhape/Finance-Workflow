/** Build-time configuration.
 *
 * These are deliberately literals rather than environment variables. The app
 * is a static bundle, so every value here ends up readable in the browser
 * regardless — putting them in env would add plumbing without adding secrecy,
 * and the plumbing is what kept breaking (`$env/dynamic` is empty in a static
 * build; `vercel.json` build env did not reach the build).
 *
 * NOTHING SECRET BELONGS IN THIS FILE. The Supabase publishable key is
 * designed to be public: it identifies the project, and row-level security
 * plus the worker's membership checks are what actually protect the data.
 * The service-role key, the Fernet encryption key and the Xero client secret
 * live only on the worker and must never appear here.
 */

export const SUPABASE_URL = 'https://zacgedjltfkiyfydghtp.supabase.co';
export const SUPABASE_ANON_KEY = 'sb_publishable_9YgapTPLHUm-tVbPwq2AcA_JpMgf-66';

/** Where the worker lives — deliberately empty, i.e. same origin.
 *
 * In dev, Vite proxies /api to localhost:8000. In production, Render's static
 * site rewrites /api/* to the worker service. Both are same-origin from the
 * browser's point of view, so there is no CORS to configure, no cross-origin
 * auth header handling, and no per-environment value to keep in step.
 *
 * Only set this if the web app is ever served from a different host to the
 * worker — in which case the worker's FW_WEB_ORIGIN must allow that origin.
 */
export const API_BASE = '';
