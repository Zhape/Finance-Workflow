<script>
	/** The OAuth application an org connects through, for one provider.
	 *
	 * One component for Xero and Google because the shape is identical: a
	 * client id, a secret that is never returned, and a fallback to the
	 * platform's app. Two copies of this would drift, and the half that drifted
	 * would be the one handling someone's credentials.
	 */
	import { api } from '$lib/api.js';

	let {
		provider,
		title,
		portal,
		portalLabel,
		redirectUri,
		app = null,
		isAdmin = false,
		onchange = () => {}
	} = $props();

	let clientId = $state('');
	let clientSecret = $state('');
	let appLabel = $state('');
	let busy = $state(false);
	let notice = $state('');
	let error = $state('');
	let open = $state(false);

	const own = $derived(app?.source === 'org');
	// Several apps per provider. Xero caps an unpublished app at two
	// organisations, so a customer with three needs more than one registration.
	const apps = $derived(app?.apps ?? []);
	const multiple = $derived(provider === 'xero');

	async function save(event) {
		event.preventDefault();
		busy = true;
		notice = '';
		error = '';
		try {
			const res = await api.saveProviderApp(
				provider,
				clientId.trim(),
				clientSecret,
				appLabel.trim() || null
			);
			// Never keep a secret in component state past the request that used it.
			clientSecret = '';
			clientId = '';
			appLabel = '';
			open = false;
			notice =
				res.disconnected?.length > 0
					? `Saved. ${res.disconnected.length} existing connection(s) were dropped — a token issued by the previous app cannot be refreshed by this one, so reconnect below.`
					: 'Saved. This organisation now connects through your own app.';
			await onchange();
		} catch (e) {
			error = e.message;
		}
		busy = false;
	}

	async function revert() {
		busy = true;
		notice = '';
		error = '';
		try {
			const res = await api.clearProviderApp(provider);
			notice =
				res.disconnected?.length > 0
					? `Reverted to the shared application. ${res.disconnected.length} connection(s) were dropped — reconnect below.`
					: 'Reverted to the shared application.';
			await onchange();
		} catch (e) {
			error = e.message;
		}
		busy = false;
	}
</script>

<div class="app">
	<div class="head">
		<div>
			<div class="name">{title}</div>
			<div class="meta">
				{#if own}
					Using <strong>this organisation's own app</strong> · Client ID
					<code>{app.clientId}</code>
					{#if app.updatedBy}· set by {app.updatedBy}{/if}
				{:else}
					Using the <strong>shared application</strong> provided by this deployment.
				{/if}
			</div>
		</div>
		<div class="actions">
			{#if own && isAdmin}
				<button class="ghost" disabled={busy} onclick={revert}>Use shared app</button>
			{/if}
			{#if isAdmin}
				<button class="ghost" onclick={() => (open = !open)} aria-expanded={open}>
					{open ? 'Cancel' : own ? 'Replace' : 'Use my own'}
				</button>
			{/if}
		</div>
	</div>

	{#if own && multiple}
		<ul class="apps">
			{#each apps as registered (registered.name)}
				<li>
					<span class="app-name">{registered.label || registered.name}</span>
					<code>{registered.clientId}</code>
					<span class="cap" class:full={registered.full}>
						{registered.organisations.length} / {registered.capacity} organisations
					</span>
				</li>
			{/each}
		</ul>
	{/if}

	{#if notice}<div class="banner ok">{notice}</div>{/if}
	{#if error}<div class="banner danger">{error}</div>{/if}

	{#if open && isAdmin}
		<form onsubmit={save}>
			<p class="steps">
				Create an OAuth client in
				<a href={portal} target="_blank" rel="noreferrer noopener">{portalLabel}</a>, add this
				redirect URI to it, then paste the credentials here.
			</p>
			{#if redirectUri}
				<code class="uri">{redirectUri}</code>
			{/if}

			{#if multiple}
				<label for="{provider}-label">Name for this application</label>
				<input
					id="{provider}-label"
					type="text"
					autocomplete="off"
					bind:value={appLabel}
					placeholder="UK &amp; EU app"
					required
				/>
			{/if}

			<label for="{provider}-id">Client ID</label>
			<input
				id="{provider}-id"
				type="text"
				autocomplete="off"
				spellcheck="false"
				bind:value={clientId}
				required
			/>

			<label for="{provider}-secret">Client secret</label>
			<input
				id="{provider}-secret"
				type="password"
				autocomplete="new-password"
				bind:value={clientSecret}
				required
			/>

			<button type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save credentials'}</button>
			<p class="help">
				Encrypted before storage and never shown back — not even to you. Other organisations
				keep their own.
			</p>
		</form>
	{/if}
</div>

<style>
	.app {
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 10px;
		padding: 16px;
		margin-bottom: 12px;
	}
	.head {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 16px;
	}
	.name {
		font-weight: 600;
		font-size: 0.9rem;
	}
	.meta {
		font-size: 0.8rem;
		color: var(--muted);
		margin-top: 3px;
		line-height: 1.5;
	}
	.actions {
		display: flex;
		gap: 8px;
		flex-shrink: 0;
	}
	.banner {
		padding: 10px 12px;
		border-radius: 6px;
		font-size: 0.83rem;
		margin-top: 12px;
		line-height: 1.5;
	}
	.banner.ok {
		background: #e7f6ec;
		color: #116329;
	}
	.banner.danger {
		background: #fdecea;
		color: var(--danger);
	}
	form {
		display: grid;
		gap: 6px;
		margin-top: 14px;
		max-width: 460px;
	}
	.steps {
		font-size: 0.82rem;
		color: var(--muted);
		margin: 0 0 8px;
		line-height: 1.5;
	}
	.uri {
		background: #14181d;
		color: #d6dde5;
		padding: 8px 10px;
		border-radius: 6px;
		font-size: 0.76rem;
		overflow-x: auto;
		white-space: nowrap;
		margin-bottom: 12px;
	}
	label {
		font-size: 0.8rem;
		font-weight: 600;
	}
	input {
		padding: 8px 10px;
		border: 1px solid var(--line);
		border-radius: 6px;
		font: inherit;
		font-size: 0.85rem;
		box-sizing: border-box;
		margin-bottom: 8px;
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
		justify-self: start;
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
	.help {
		font-size: 0.78rem;
		color: var(--muted);
		margin: 8px 0 0;
		line-height: 1.5;
	}
	.apps {
		list-style: none;
		margin: 12px 0 0;
		padding: 0;
		display: grid;
		gap: 6px;
		font-size: 0.82rem;
	}
	.apps li {
		display: flex;
		align-items: center;
		gap: 10px;
		flex-wrap: wrap;
		padding: 8px 10px;
		background: #f6f7f9;
		border-radius: 6px;
	}
	.app-name {
		font-weight: 600;
	}
	.cap {
		margin-left: auto;
		color: var(--muted);
		font-size: 0.78rem;
	}
	.cap.full {
		color: var(--warn);
		font-weight: 600;
	}
	code {
		background: #eef1f5;
		padding: 1px 5px;
		border-radius: 4px;
		font-size: 0.85em;
	}
</style>
