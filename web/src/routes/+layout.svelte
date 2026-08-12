<script>
	import { onMount } from 'svelte';
	import { api } from '$lib/api.js';
	import {
		applyIdentity,
		session,
		signInWithPassword,
		signOut,
		supabaseConfigured
	} from '$lib/session.svelte.js';

	let { children } = $props();

	let ready = $state(false);
	let authError = $state('');
	let email = $state('');
	let password = $state('');
	let signingIn = $state(false);

	onMount(loadIdentity);

	async function loadIdentity() {
		try {
			applyIdentity(await api.me());
			authError = '';
		} catch (e) {
			// 401/403 are "not signed in", not failures worth shouting about.
			authError = e.status === 401 || e.status === 403 ? '' : e.message;
			session.email = null;
		}
		ready = true;
	}

	async function submit(event) {
		event.preventDefault();
		signingIn = true;
		authError = '';
		try {
			await signInWithPassword(email, password);
			// Never keep the password in memory past the request that used it.
			password = '';
			await loadIdentity();
		} catch (e) {
			authError = e.message;
		}
		signingIn = false;
	}

	function leave() {
		signOut();
		email = '';
		password = '';
	}
</script>

<div class="shell">
	<header>
		<a class="brand" href="/">Finance Workflows</a>
		{#if session.email}
			<span class="org">
				<strong>{session.orgName}</strong>
				· {session.email}
				<span class="role">{session.role}</span>
				<a href="/settings">Settings</a>
				{#if supabaseConfigured}
					<button class="link" onclick={leave}>Sign out</button>
				{/if}
			</span>
		{/if}
	</header>

	<main>
		{#if !ready}
			<p class="muted">Loading…</p>
		{:else if session.email}
			{@render children()}
		{:else if supabaseConfigured && !session.accessToken}
			<!-- Sign-in comes before any status message. Nothing about the
			     deployment — not even which integrations are missing — is worth
			     showing to someone who has not signed in. -->
			<div class="signin">
				<h1>Sign in</h1>
				<p class="muted">This tool reaches company financial data. Accounts are issued by an
					administrator — there is no self sign-up.</p>
				<form onsubmit={submit}>
					<label for="email">Email</label>
					<input
						id="email"
						type="email"
						autocomplete="username"
						bind:value={email}
						placeholder="you@company.com"
						required
					/>

					<label for="password">Password</label>
					<input
						id="password"
						type="password"
						autocomplete="current-password"
						bind:value={password}
						required
					/>

					<button type="submit" disabled={signingIn}>
						{signingIn ? 'Signing in…' : 'Sign in'}
					</button>
				</form>
				{#if authError}<p class="err">{authError}</p>{/if}
			</div>
		{:else if supabaseConfigured}
			<!-- Signed in to Supabase, but the worker did not accept the session:
			     a valid token with no org_members row. -->
			<div class="signin">
				<h1>No access</h1>
				<p class="muted">
					You are signed in as {session.email ?? 'this account'}, but it is not a member of
					any organisation. Ask an administrator to add you.
				</p>
				{#if authError}<p class="err">{authError}</p>{/if}
				<button onclick={leave}>Sign out</button>
			</div>
		{:else}
			<div class="signin">
				<h1>Not signed in</h1>
				<p class="muted">
					Supabase auth is not configured, and the worker's dev user is not a member of any
					organisation. Run <code>python -m fw.seed --email you@company.com</code>.
				</p>
				{#if authError}<p class="err">{authError}</p>{/if}
			</div>
		{/if}
	</main>
</div>

<style>
	:global(:root) {
		--bg: #f6f7f9;
		--card: #fff;
		--line: #e2e5ea;
		--text: #1a1d21;
		--muted: #656d78;
		--accent: #0078d7;
		--warn: #9a6700;
		--warn-bg: #fff8e5;
		--danger: #b42318;
		font-family: 'Segoe UI', system-ui, sans-serif;
		color: var(--text);
	}
	:global(body) {
		margin: 0;
		background: var(--bg);
	}
	:global(a) {
		color: var(--accent);
	}
	.shell {
		max-width: 1100px;
		margin: 0 auto;
		padding: 0 24px 64px;
	}
	header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 16px;
		padding: 20px 0;
		margin-bottom: 8px;
		border-bottom: 1px solid var(--line);
	}
	.brand {
		font-weight: 650;
		font-size: 1.05rem;
		text-decoration: none;
		color: var(--text);
	}
	.org {
		font-size: 0.82rem;
		color: var(--muted);
		display: flex;
		align-items: center;
		gap: 10px;
	}
	.role {
		font-size: 0.7rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		background: #eef1f5;
		padding: 2px 7px;
		border-radius: 999px;
	}
	.muted {
		color: var(--muted);
	}
	.err {
		color: var(--danger);
		font-size: 0.85rem;
	}
	.signin {
		max-width: 420px;
		margin: 60px auto;
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 10px;
		padding: 28px;
	}
	.signin h1 {
		font-size: 1.2rem;
		margin: 0 0 8px;
	}
	form {
		display: grid;
		gap: 6px;
		margin-top: 20px;
	}
	form label {
		font-size: 0.82rem;
		font-weight: 600;
	}
	form label + input {
		margin-bottom: 10px;
	}
	input {
		padding: 9px 10px;
		border: 1px solid var(--line);
		border-radius: 6px;
		font: inherit;
		box-sizing: border-box;
	}
	form button {
		margin-top: 6px;
		justify-self: start;
	}
	form button:disabled {
		opacity: 0.6;
		cursor: default;
	}
	button {
		font: inherit;
		font-weight: 600;
		padding: 8px 16px;
		border: none;
		border-radius: 6px;
		background: var(--accent);
		color: #fff;
		cursor: pointer;
	}
	button.link {
		background: none;
		color: var(--accent);
		padding: 0;
		font-weight: 400;
		font-size: 0.82rem;
	}
	code {
		background: #eef1f5;
		padding: 1px 5px;
		border-radius: 4px;
		font-size: 0.85em;
	}
</style>
