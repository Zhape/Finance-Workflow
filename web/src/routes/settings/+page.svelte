<script>
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { api } from '$lib/api.js';
	import { session } from '$lib/session.svelte.js';

	// Each region draws from a named Xero connection. UK and EU share one Xero
	// organisation; the US is separate, mirroring how the accounts are held.
	const SLOTS = [
		{ name: 'default', label: 'Xero — UK & EU' },
		{ name: 'us', label: 'Xero — US' }
	];

	const ERRORS = {
		expired_state: 'That sign-in took too long. Start again.',
		exchange_failed: 'Xero rejected the authorisation. Try again.',
		no_organisations: 'That Xero login has no organisations attached.',
		missing_code: 'Xero did not return an authorisation code.',
		access_denied: 'Consent was declined.'
	};

	let connections = $state([]);
	let busy = $state('');
	let error = $state('');
	let loaded = $state(false);

	const isAdmin = $derived(session.role === 'admin');
	const notice = $derived($page.url.searchParams.get('connected'));
	const callbackError = $derived($page.url.searchParams.get('error'));

	onMount(refresh);

	async function refresh() {
		try {
			connections = (await api.connections()).connections;
		} catch (e) {
			error = e.message;
		}
		loaded = true;
	}

	function connectedTo(name) {
		return connections.find((c) => c.name === name);
	}

	async function connect(name) {
		busy = name;
		error = '';
		try {
			// The worker mints the PKCE challenge and holds the verifier; we only
			// ever receive the URL to send the person to.
			const { url } = await api.connectXero(name);
			window.location.href = url;
		} catch (e) {
			error = e.message;
			busy = '';
		}
	}

	async function disconnect(name) {
		busy = name;
		error = '';
		try {
			await api.disconnect(name);
			await refresh();
		} catch (e) {
			error = e.message;
		}
		busy = '';
	}
</script>

<p class="crumb"><a href="/">← All workflows</a></p>
<h1>Settings</h1>
<p class="lede">Connections for <strong>{session.orgName}</strong>.</p>

{#if notice}
	<div class="banner ok">Connected {notice}.</div>
{/if}
{#if callbackError}
	<div class="banner danger">{ERRORS[callbackError] ?? callbackError}</div>
{/if}
{#if error}
	<div class="banner danger">{error}</div>
{/if}

<h2>Accounting</h2>
{#if !isAdmin}
	<p class="muted">Only an admin can connect or disconnect integrations.</p>
{/if}

<div class="rows">
	{#each SLOTS as slot (slot.name)}
		{@const existing = connectedTo(slot.name)}
		<div class="row">
			<div>
				<div class="name">{slot.label}</div>
				{#if existing}
					<div class="meta">
						{existing.tenantName ?? 'Connected'}
						· by {existing.connectedBy ?? 'unknown'}
					</div>
				{:else if loaded}
					<div class="meta">Not connected</div>
				{/if}
			</div>
			<div class="actions">
				{#if existing}
					<span class="pill ok">Connected</span>
					<button
						class="ghost"
						disabled={!isAdmin || busy === slot.name}
						onclick={() => disconnect(slot.name)}
					>
						Disconnect
					</button>
				{:else}
					<button disabled={!isAdmin || busy === slot.name} onclick={() => connect(slot.name)}>
						{busy === slot.name ? 'Opening Xero…' : 'Connect Xero'}
					</button>
				{/if}
			</div>
		</div>
	{/each}
</div>

<p class="note">
	Connecting grants this app read access to your Xero organisation. Tokens are encrypted before
	they are stored and are never shown back to anyone, including you.
</p>

<style>
	.crumb {
		margin: 20px 0 0;
		font-size: 0.85rem;
	}
	.crumb a {
		text-decoration: none;
	}
	h1 {
		font-size: 1.35rem;
		margin: 12px 0 4px;
	}
	h2 {
		font-size: 1rem;
		margin: 32px 0 12px;
	}
	.lede,
	.muted {
		color: var(--muted);
		margin: 0 0 12px;
	}
	.banner {
		padding: 11px 14px;
		border-radius: 8px;
		font-size: 0.87rem;
		margin-bottom: 12px;
	}
	.banner.ok {
		background: #e7f6ec;
		color: #116329;
	}
	.banner.danger {
		background: #fdecea;
		color: var(--danger);
	}
	.rows {
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 10px;
		overflow: hidden;
	}
	.row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 14px 16px;
		border-bottom: 1px solid var(--line);
	}
	.row:last-child {
		border-bottom: none;
	}
	.name {
		font-weight: 600;
		font-size: 0.9rem;
	}
	.meta {
		font-size: 0.8rem;
		color: var(--muted);
		margin-top: 2px;
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 10px;
	}
	.pill {
		font-size: 0.72rem;
		padding: 2px 8px;
		border-radius: 999px;
	}
	.pill.ok {
		background: #e7f6ec;
		color: #116329;
	}
	button {
		font: inherit;
		font-size: 0.85rem;
		font-weight: 600;
		padding: 7px 14px;
		border: none;
		border-radius: 6px;
		background: var(--accent);
		color: #fff;
		cursor: pointer;
	}
	button.ghost {
		background: none;
		color: var(--muted);
		border: 1px solid var(--line);
	}
	button:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.note {
		font-size: 0.8rem;
		color: var(--muted);
		margin-top: 16px;
	}
</style>
