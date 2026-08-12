/** Who is signed in, and which org they are acting for.
 *
 * Supabase Auth is the identity provider: email + password, verified by
 * Supabase, never by this app. No password is stored, logged, or sent
 * anywhere except Supabase's own token endpoint over TLS.
 *
 * There is deliberately no sign-up form. Accounts are created by an admin in
 * the Supabase dashboard and granted membership in `org_members`; a public
 * sign-up on a tool that reaches company bank details would be a mistake.
 * Even if someone did obtain an account, the worker would refuse them: a JWT
 * proves identity, but membership decides access, and a user with no
 * membership row gets a 403.
 */

// Dynamic rather than static: these are unset in local dev, and static env
// turns a missing variable into a build failure.
import { env } from '$env/dynamic/public';

const SUPABASE_URL = (env.PUBLIC_SUPABASE_URL ?? '').replace(/\/$/, '');
const SUPABASE_ANON_KEY = env.PUBLIC_SUPABASE_ANON_KEY ?? '';

export const supabaseConfigured = Boolean(SUPABASE_URL && SUPABASE_ANON_KEY);

const TOKEN_KEY = 'fw.token';
const REFRESH_KEY = 'fw.refresh';

export const session = $state({
	accessToken: null,
	refreshToken: null,
	email: null,
	orgId: null,
	orgName: null,
	role: null
});

export function applyIdentity(me) {
	session.email = me.email;
	session.orgId = me.org.id;
	session.orgName = me.org.name;
	session.role = me.role;
}

function store(tokens) {
	session.accessToken = tokens.access_token ?? null;
	session.refreshToken = tokens.refresh_token ?? null;
	if (typeof window === 'undefined') return;
	// sessionStorage, not localStorage: the session dies with the tab rather
	// than persisting on a shared machine.
	if (session.accessToken) sessionStorage.setItem(TOKEN_KEY, session.accessToken);
	if (session.refreshToken) sessionStorage.setItem(REFRESH_KEY, session.refreshToken);
}

async function tokenRequest(grant, body) {
	const res = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=${grant}`, {
		method: 'POST',
		headers: { 'content-type': 'application/json', apikey: SUPABASE_ANON_KEY },
		body: JSON.stringify(body)
	});
	if (!res.ok) {
		let message = 'Sign-in failed.';
		try {
			const detail = await res.json();
			// Supabase says "Invalid login credentials" for both a wrong password
			// and an unknown email, which is the correct behaviour: it does not
			// reveal whether an account exists. Pass it through unchanged.
			message = detail.error_description || detail.msg || detail.error || message;
		} catch {
			/* keep the generic message */
		}
		const error = new Error(message);
		error.status = res.status;
		throw error;
	}
	return res.json();
}

export async function signInWithPassword(email, password) {
	if (!supabaseConfigured) throw new Error('Supabase is not configured.');
	store(await tokenRequest('password', { email, password }));
}

/** Swap an expired access token for a fresh one. Returns false if the refresh
 *  token is gone or rejected, in which case the caller should show sign-in. */
export async function refreshSession() {
	if (!supabaseConfigured || !session.refreshToken) return false;
	try {
		store(await tokenRequest('refresh_token', { refresh_token: session.refreshToken }));
		return true;
	} catch {
		signOut();
		return false;
	}
}

export function restoreToken() {
	if (typeof window === 'undefined') return;
	session.accessToken = sessionStorage.getItem(TOKEN_KEY);
	session.refreshToken = sessionStorage.getItem(REFRESH_KEY);
}

// Restore at module load, not in onMount: page `load` functions run before any
// component mounts, so waiting would send the first request unauthenticated.
if (typeof window !== 'undefined') restoreToken();

export function signOut() {
	session.accessToken = null;
	session.refreshToken = null;
	session.email = null;
	session.orgId = null;
	session.orgName = null;
	session.role = null;
	if (typeof window === 'undefined') return;
	sessionStorage.removeItem(TOKEN_KEY);
	sessionStorage.removeItem(REFRESH_KEY);
}
