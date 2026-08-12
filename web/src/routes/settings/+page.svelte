<script>
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { api } from '$lib/api.js';
	import { session } from '$lib/session.svelte.js';

	// Each region draws from a named Xero connection. UK and EU share one Xero
	// organisation; the US is separate, mirroring how the accounts are held.
	const SLOTS = [
		{
			name: 'default',
			label: 'Xero — UK & EU',
			hint: 'Sign in as the Xero user who can see the UK organisation. EU pay runs read from the same one.'
		},
		{
			name: 'us',
			label: 'Xero — US',
			hint: 'A separate Xero organisation. If you only have one, connect it here too and pick it at the org chooser.'
		}
	];

	const ERRORS = {
		expired_state: 'That took longer than 10 minutes, so the request expired. Start again.',
		exchange_failed:
			'Xero rejected the authorisation. The usual cause is the redirect URI not matching the one registered on the Xero app — check step 2 below.',
		no_organisations:
			'That Xero login has no organisations attached. Sign in with an account that has access to the company file.',
		missing_code: 'Xero did not return an authorisation code. Start again.',
		access_denied: 'Consent was declined, so nothing was connected.'
	};

	let connections = $state([]);
	let setup = $state(null);
	let busy = $state('');
	let error = $state('');
	let loaded = $state(false);
	let copied = $state(false);
	let showGuide = $state(false);

	const isAdmin = $derived(session.role === 'admin');
	const notice = $derived($page.url.searchParams.get('connected'));
	const callbackError = $derived($page.url.searchParams.get('error'));
	const allConnected = $derived(
		loaded && SLOTS.every((s) => connections.some((c) => c.name === s.name))
	);

	onMount(async () => {
		await refresh();
		try {
			setup = await api.xeroSetup();
		} catch {
			setup = null;
		}
		// Open the walkthrough by default when there is something left to connect,
		// and after a failed callback — those are the moments it is needed.
		showGuide = !allConnected || Boolean(callbackError);
	});

	async function refresh() {
		try {
			connections = (await api.connections()).connections;
		} catch (e) {
			error = e.message;
		}
		loaded = true;
	}

	const connectedTo = (name) => connections.find((c) => c.name === name);

	async function copyRedirect() {
		if (!setup?.redirectUri) return;
		try {
			await navigator.clipboard.writeText(setup.redirectUri);
			copied = true;
			setTimeout(() => (copied = false), 2000);
		} catch {
			copied = false;
		}
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
	<div class="banner ok">Connected {notice}. Pay runs for that region can now read from Xero.</div>
{/if}
{#if callbackError}
	<div class="banner danger">{ERRORS[callbackError] ?? callbackError}</div>
{/if}
{#if error}
	<div class="banner danger">{error}</div>
{/if}

<h2>Connect Xero</h2>

{#if setup && !setup.appConfigured}
	<div class="banner danger">
		This deployment has no Xero application configured, so connecting cannot work yet.
		<code>FW_XERO_CLIENT_ID</code> and <code>FW_XERO_CLIENT_SECRET</code> need setting on the worker.
	</div>
{/if}

<div class="guide" class:open={showGuide}>
	<button class="guidehead" onclick={() => (showGuide = !showGuide)} aria-expanded={showGuide}>
		<span class="chev" aria-hidden="true">{showGuide ? '▾' : '▸'}</span>
		<span>How to connect Xero{allConnected ? ' (already done)' : ''}</span>
	</button>

	{#if showGuide}
		<ol class="steps">
			<li>
				<strong>Open your Xero app</strong>
				<p>
					Go to
					<a href={setup?.developerPortal ?? 'https://developer.xero.com/app/manage'}
						target="_blank" rel="noreferrer noopener">developer.xero.com → My Apps</a>
					and open the app used for this tool. If there isn't one, create it as a
					<em>Web app</em>.
				</p>
			</li>

			<li>
				<strong>Add this redirect URI — exactly</strong>
				<p>
					In the app's <em>Configuration</em> tab, paste this into Redirect URIs and save. Xero
					compares it character for character; a trailing slash or <code>http</code> instead of
					<code>https</code> is enough to be rejected at the end of the flow.
				</p>
				{#if setup?.redirectUri}
					<div class="copyrow">
						<code class="uri">{setup.redirectUri}</code>
						<button class="ghost small" onclick={copyRedirect}>{copied ? 'Copied' : 'Copy'}</button>
					</div>
				{:else}
					<p class="muted">Could not read the redirect URI from the worker.</p>
				{/if}
			</li>

			<li>
				<strong>Come back and press Connect</strong>
				<p>
					Xero will ask which organisation to grant access to, then return you here. The
					consent screen lists read <em>and</em> write permissions across accounting — granted
					up front so later workflows don't send everyone back through consent. Nothing in
					this app writes to Xero today, and pay runs can't move money: they produce a file
					you upload to the bank yourself.
				</p>
				{#if setup?.scopes?.length}
					<details class="scopes">
						<summary>What it asks for ({setup.scopes.length} scopes)</summary>
						<ul>
							{#each setup.scopes as scope}
								<li><code>{scope}</code></li>
							{/each}
						</ul>
					</details>
				{/if}
			</li>
		</ol>
	{/if}
</div>

<div class="rows">
	{#each SLOTS as slot (slot.name)}
		{@const existing = connectedTo(slot.name)}
		<div class="row">
			<div class="who">
				<div class="name">{slot.label}</div>
				{#if existing}
					<div class="meta">
						Connected to <strong>{existing.tenantName ?? 'a Xero organisation'}</strong>
						· by {existing.connectedBy ?? 'unknown'}
					</div>
				{:else if loaded}
					<div class="meta">{slot.hint}</div>
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
					<button
						disabled={!isAdmin || busy === slot.name || (setup && !setup.appConfigured)}
						onclick={() => connect(slot.name)}
					>
						{busy === slot.name ? 'Opening Xero…' : 'Connect Xero'}
					</button>
				{/if}
			</div>
		</div>
	{/each}
</div>

{#if !isAdmin}
	<p class="note">Only an admin can connect or disconnect integrations. Yours is {session.role}.</p>
{/if}

<p class="note">
	Tokens are encrypted before they are stored and are never shown back to anyone, including you.
	Disconnecting deletes this organisation's token; it does not revoke the app inside Xero, which is
	done from Xero's own connected-apps screen.
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
	.lede {
		color: var(--muted);
		margin: 0 0 12px;
	}
	.banner {
		padding: 11px 14px;
		border-radius: 8px;
		font-size: 0.87rem;
		margin-bottom: 12px;
		line-height: 1.45;
	}
	.banner.ok {
		background: #e7f6ec;
		color: #116329;
	}
	.banner.danger {
		background: #fdecea;
		color: var(--danger);
	}

	.guide {
		border: 1px solid var(--line);
		border-radius: 10px;
		background: var(--card);
		margin-bottom: 14px;
		overflow: hidden;
	}
	.guidehead {
		width: 100%;
		display: flex;
		align-items: center;
		gap: 8px;
		background: none;
		border: none;
		padding: 13px 16px;
		font: inherit;
		font-weight: 600;
		font-size: 0.9rem;
		color: var(--text);
		cursor: pointer;
		text-align: left;
	}
	.guide.open .guidehead {
		border-bottom: 1px solid var(--line);
	}
	.chev {
		color: var(--muted);
		font-size: 0.8rem;
	}
	.steps {
		margin: 0;
		padding: 14px 16px 16px 34px;
		font-size: 0.87rem;
		line-height: 1.5;
	}
	.steps li + li {
		margin-top: 16px;
	}
	.steps p {
		margin: 4px 0 0;
		color: var(--muted);
	}
	.copyrow {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-top: 8px;
		flex-wrap: wrap;
	}
	.uri {
		flex: 1;
		min-width: 240px;
		background: #14181d;
		color: #d6dde5;
		padding: 9px 11px;
		border-radius: 6px;
		font-size: 0.78rem;
		overflow-x: auto;
		white-space: nowrap;
	}
	.scopes {
		margin-top: 8px;
		font-size: 0.82rem;
		color: var(--muted);
	}
	.scopes summary {
		cursor: pointer;
	}
	.scopes ul {
		margin: 6px 0 0;
		padding-left: 18px;
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
	.who {
		min-width: 0;
	}
	.name {
		font-weight: 600;
		font-size: 0.9rem;
	}
	.meta {
		font-size: 0.8rem;
		color: var(--muted);
		margin-top: 3px;
		line-height: 1.45;
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 10px;
		flex-shrink: 0;
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
	button.small {
		padding: 6px 12px;
		font-size: 0.8rem;
	}
	button:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.note {
		font-size: 0.8rem;
		color: var(--muted);
		margin-top: 14px;
		line-height: 1.5;
	}
	code {
		background: #eef1f5;
		padding: 1px 5px;
		border-radius: 4px;
		font-size: 0.85em;
	}
</style>
