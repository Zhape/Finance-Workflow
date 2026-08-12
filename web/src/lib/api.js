/** Thin client for the worker API.
 *
 * Two headers matter. `Authorization` carries the Supabase access token and
 * proves who you are. `X-Org-Id` says which org you want to act for — it is a
 * request, not a grant: the worker honours it only if your membership backs
 * it up, so a tampered header gets a 404, not someone else's data.
 */

import { API_BASE as CONFIGURED_API_BASE } from './config.js';
import { refreshSession, session } from './session.svelte.js';

export const API_BASE = CONFIGURED_API_BASE.replace(/\/$/, '');

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

	// A 200 that isn't JSON means the request never reached the worker — the
	// SPA catch-all answered it with index.html. Say that, rather than letting
	// a raw "Unexpected token '<'" reach someone who cannot act on it.
	if (!(res.headers.get('content-type') || '').includes('json')) {
		const error = new Error(
			'The API returned a page instead of data — the /api route is not reaching the worker.'
		);
		error.status = 502;
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
	xeroSetup: (fetcher = fetch) => get('/api/connections/xero/setup', fetcher),
	// Provider-agnostic: 'xero' or 'google'. The worker rejects anything else.
	saveProviderApp: (provider, clientId, clientSecret, label = null) =>
		send(`/api/connections/${encodeURIComponent(provider)}/app`, 'PUT', {
			clientId,
			clientSecret,
			label
		}),
	clearProviderApp: (provider) =>
		send(`/api/connections/${encodeURIComponent(provider)}/app`, 'DELETE'),

	googleSetup: (fetcher = fetch) => get('/api/connections/google/setup', fetcher),
	connectGoogle: () => send('/api/connections/google/start', 'POST'),
	disconnectGoogle: () => send('/api/connections/google', 'DELETE'),

	templates: (key, fetcher = fetch) =>
		get(`/api/workflows/${encodeURIComponent(key)}/templates`, fetcher),
	saveTemplate: (key, variant, subject, body) =>
		send(`/api/workflows/${encodeURIComponent(key)}/templates`, 'PUT', {
			variant,
			subject,
			body
		}),
	resetTemplate: (key, variant) =>
		send(
			`/api/workflows/${encodeURIComponent(key)}/templates/${encodeURIComponent(variant)}`,
			'DELETE'
		),

	// Invoice Inbox. Ingestion is a button, so `syncInbox` is the only thing
	// that pulls mail — there is no background job filling this screen behind
	// your back, and nothing here sends: `draftReply` puts a draft in Gmail.
	inboxStatus: (fetcher = fetch) => get('/api/inbox/status', fetcher),
	inboxEmails: (fetcher = fetch) => get('/api/inbox/emails', fetcher),
	inboxEmail: (id, fetcher = fetch) =>
		get(`/api/inbox/emails/${encodeURIComponent(id)}`, fetcher),
	syncInbox: () => send('/api/inbox/sync', 'POST'),
	connectMailbox: () => send('/api/inbox/mailboxes/start', 'POST'),
	removeMailbox: (id) => send(`/api/inbox/mailboxes/${encodeURIComponent(id)}`, 'DELETE'),
	setInboxCategory: (id, categoryKey) =>
		send(`/api/inbox/emails/${encodeURIComponent(id)}/category`, 'PUT', { categoryKey }),
	saveInboxDraft: (id, subject, body) =>
		send(`/api/inbox/emails/${encodeURIComponent(id)}/draft`, 'PUT', { subject, body }),
	draftReply: (id) => send(`/api/inbox/emails/${encodeURIComponent(id)}/draft-reply`, 'POST'),
	dismissInboxEmail: (id) =>
		send(`/api/inbox/emails/${encodeURIComponent(id)}/dismiss`, 'POST'),
	saveInboxSettings: (lookbackDays, xeroConnection) =>
		send('/api/inbox/settings', 'PUT', { lookbackDays, xeroConnection }),
	inboxTemplates: (fetcher = fetch) => get('/api/inbox/templates', fetcher),
	saveInboxTemplate: (variant, subject, body) =>
		send('/api/inbox/templates', 'PUT', { variant, subject, body }),
	resetInboxTemplate: (variant) =>
		send(`/api/inbox/templates/${encodeURIComponent(variant)}`, 'DELETE'),
	addInboxCategory: (key, label, description) =>
		send('/api/inbox/categories', 'POST', { key, label, description }),
	editInboxCategory: (key, changes) =>
		send(`/api/inbox/categories/${encodeURIComponent(key)}`, 'PUT', changes),
	dismissInboxErrors: () => send('/api/inbox/errors/dismiss', 'POST'),

	workflowRequests: (fetcher = fetch) => get('/api/workflow-requests', fetcher),
	requestWorkflow: (title, description) =>
		send('/api/workflow-requests', 'POST', { title, description }),

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

/** How long something has been waiting, in the units a person would say it in.
 *  The left-hand queue is sorted by this, so it has to read at a glance. */
export function age(iso) {
	if (!iso) return '—';
	const hasZone = /[Z+]|-\d{2}:\d{2}$/.test(iso.slice(10));
	const then = new Date(hasZone ? iso : iso + 'Z');
	if (Number.isNaN(then.getTime())) return '—';

	const minutes = Math.max(0, Math.round((Date.now() - then.getTime()) / 60000));
	if (minutes < 1) return 'just now';
	if (minutes < 60) return `${minutes}m`;
	const hours = Math.round(minutes / 60);
	if (hours < 48) return `${hours}h`;
	return `${Math.round(hours / 24)}d`;
}

export function money(value) {
	return Number(value ?? 0).toLocaleString('en-GB', {
		minimumFractionDigits: 2,
		maximumFractionDigits: 2
	});
}
