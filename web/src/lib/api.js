/** Thin client for the worker API.
 *
 * Two headers matter. `Authorization` carries the Supabase access token and
 * proves who you are. `X-Org-Id` says which org you want to act for — it is a
 * request, not a grant: the worker honours it only if your membership backs
 * it up, so a tampered header gets a 404, not someone else's data.
 */

import { env } from '$env/dynamic/public';

import { refreshSession, session } from './session.svelte.js';

/** Where the worker lives.
 *
 * Empty in dev: Vite proxies /api to localhost:8000. In production there is no
 * proxy and the worker is on its own host (it cannot be a Vercel function — a
 * Xero pull outlives the timeout), so the browser calls it directly and the
 * worker allows the origin via FW_WEB_ORIGIN.
 */
export const API_BASE = (env.PUBLIC_API_BASE ?? '').replace(/\/$/, '');
export const workerConfigured = Boolean(API_BASE) || !import.meta.env.PROD;

const url = (path) => `${API_BASE}${path}`;

function headers(extra = {}) {
	const out = { ...extra };
	if (session.accessToken) out.authorization = `Bearer ${session.accessToken}`;
	if (session.orgId) out['x-org-id'] = session.orgId;
	return out;
}

async function json(res) {
	if (!res.ok) {
		let detail;
		try {
			detail = (await res.clone().json()).detail;
		} catch {
			detail = (await res.text()) || res.statusText;
		}
		const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
		error.status = res.status;
		throw error;
	}
	return res.json();
}

/** Run a request, and on a 401 try one token refresh before giving up.
 *
 * Supabase access tokens are short-lived (an hour by default). Without this,
 * someone mid-review would be bounced to the sign-in screen and lose the
 * rows they had ticked. Only 401 is retried, and only once — a 403 is a
 * permissions answer, not a stale token, and retrying it would be a loop. */
async function withRefresh(run) {
	let res = await run();
	if (res.status === 401 && (await refreshSession())) {
		res = await run();
	}
	return json(res);
}

const get = (path, fetcher = fetch) =>
	withRefresh(() => fetcher(url(path), { headers: headers() }));

const send = (path, method, body) =>
	withRefresh(() =>
		fetch(url(path), {
			method,
			headers: headers({ 'content-type': 'application/json' }),
			body: body === undefined ? undefined : JSON.stringify(body)
		})
	);

export const api = {
	me: (fetcher = fetch) => get('/api/me', fetcher),
	workflows: (fetcher = fetch) => get('/api/workflows', fetcher),
	runs: (fetcher = fetch) => get('/api/runs', fetcher),
	run: (id, fetcher = fetch) => get(`/api/runs/${id}`, fetcher),
	connections: (fetcher = fetch) => get('/api/connections', fetcher),

	start: (workflow, params) => send('/api/runs', 'POST', { workflow, params }),
	approve: (id, rowIds) => send(`/api/runs/${id}/approve`, 'POST', { rowIds }),
	connectXero: (name = 'default') =>
		send(`/api/connections/xero/start?name=${encodeURIComponent(name)}`, 'POST'),
	disconnect: (name) => send(`/api/connections/${encodeURIComponent(name)}`, 'DELETE'),

	// The artifact is a normal download, but it still needs the auth headers,
	// so it is fetched as a blob rather than linked to directly.
	async downloadArtifact(id, filename) {
		const res = await fetch(url(`/api/runs/${id}/artifact`), { headers: headers() });
		if (!res.ok) throw new Error('Could not download the file.');
		const blobUrl = URL.createObjectURL(await res.blob());
		const link = document.createElement('a');
		link.href = blobUrl;
		link.download = filename ?? 'payrun.csv';
		link.click();
		URL.revokeObjectURL(blobUrl);
	}
};

/** Timestamps come back as ISO strings; show them in the reader's timezone.
 *  Postgres sends an offset, SQLite does not — treat a naive value as UTC
 *  rather than as local time, or dev timestamps drift by the offset. */
export function when(iso) {
	if (!iso) return '—';
	const hasZone = /[Z+]|-\d{2}:\d{2}$/.test(iso.slice(10));
	const date = new Date(hasZone ? iso : iso + 'Z');
	if (Number.isNaN(date.getTime())) return iso;
	return date.toLocaleString('en-GB', {
		day: '2-digit',
		month: 'short',
		hour: '2-digit',
		minute: '2-digit'
	});
}

export function money(value) {
	return Number(value ?? 0).toLocaleString('en-GB', {
		minimumFractionDigits: 2,
		maximumFractionDigits: 2
	});
}
