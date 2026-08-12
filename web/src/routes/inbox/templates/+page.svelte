<script>
	import { onMount } from 'svelte';
	import { api } from '$lib/api.js';

	let templates = $state({});
	let placeholders = $state([]);
	let categories = $state([]);
	let ready = $state(false);
	let error = $state('');
	let notice = $state('');
	let busy = $state(false);

	let editingKey = $state(null);
	let draftSubject = $state('');
	let draftBody = $state('');

	// Adding a bucket is the extensibility promise: a category plus its
	// wording, no deploy. The description is what teaches the classifier.
	let showNew = $state(false);
	let newKey = $state('');
	let newLabel = $state('');
	let newDescription = $state('');

	onMount(load);

	async function load() {
		try {
			const [{ templates: t, placeholders: p }, status] = await Promise.all([
				api.inboxTemplates(),
				api.inboxStatus()
			]);
			templates = t;
			placeholders = p;
			categories = status.categories;
			error = '';
		} catch (e) {
			error = e.message;
		}
		ready = true;
	}

	function edit(key) {
		editingKey = key;
		draftSubject = templates[key].subject;
		draftBody = templates[key].body;
		notice = '';
	}

	async function save() {
		busy = true;
		error = '';
		try {
			const result = await api.saveInboxTemplate(editingKey, draftSubject, draftBody);
			templates = result.templates;
			notice = `Saved as version ${result.version}.`;
			editingKey = null;
		} catch (e) {
			error = e.message;
		}
		busy = false;
	}

	async function revert(key) {
		busy = true;
		error = '';
		try {
			const result = await api.resetInboxTemplate(key);
			templates = result.templates;
			notice = 'Reverted to the product default.';
		} catch (e) {
			error = e.message;
		}
		busy = false;
	}

	async function addCategory() {
		busy = true;
		error = '';
		try {
			const result = await api.addInboxCategory(newKey, newLabel, newDescription);
			categories = result.categories;
			templates = (await api.inboxTemplates()).templates;
			notice = `Added ${newLabel}. Edit its wording below before it is used.`;
			showNew = false;
			newKey = newLabel = newDescription = '';
		} catch (e) {
			error = e.message;
		}
		busy = false;
	}

	async function toggle(key, enabled) {
		busy = true;
		error = '';
		try {
			const result = await api.editInboxCategory(key, { enabled });
			categories = result.categories;
		} catch (e) {
			error = e.message;
		}
		busy = false;
	}

	function categoryFor(key) {
		return categories.find((c) => c.key === key);
	}
</script>

<p class="crumb"><a href="/inbox">← Invoice Inbox</a></p>
<h1>Reply templates</h1>
<p class="lede">
	Every reply the product can send is on this page. The classifier picks which one to use and the
	invoice record fills the blanks — it never writes a sentence of its own.
</p>

{#if notice}<p class="banner ok">{notice}</p>{/if}
{#if error}<p class="banner err">{error}</p>{/if}

{#if !ready}
	<p class="muted">Loading…</p>
{:else}
	<div class="box slots">
		<h2>Placeholders</h2>
		<p class="muted small">
			Values come from the invoice record in Xero. A placeholder with nothing to fill it stays
			visible in the draft and blocks the reply, so nothing half-finished reaches a customer.
		</p>
		<ul>
			{#each placeholders as slot (slot.token)}
				<li><code>{slot.token}</code> <span class="muted">{slot.description}</span></li>
			{/each}
		</ul>
	</div>

	<div class="head">
		<h2>Categories and their wording</h2>
		<button class="ghost" onclick={() => (showNew = !showNew)}>
			{showNew ? 'Cancel' : 'Add a category'}
		</button>
	</div>

	{#if showNew}
		<div class="box">
			<label for="key">Key</label>
			<input id="key" bind:value={newKey} placeholder="Refund" />
			<label for="label">Name</label>
			<input id="label" bind:value={newLabel} placeholder="Wants a refund" />
			<label for="description">When does this apply?</label>
			<textarea
				id="description"
				rows="3"
				bind:value={newDescription}
				placeholder="The customer is asking for money back on an invoice they have already paid."
			></textarea>
			<p class="muted small">
				This description is what the classifier reads to recognise the category, so write it
				for a reader who has never seen your business.
			</p>
			<button onclick={addCategory} disabled={busy}>Add category</button>
		</div>
	{/if}

	{#each Object.entries(templates) as [key, template] (key)}
		<div class="box template">
			<div class="template-head">
				<div>
					<h3>{template.label ?? key}</h3>
					<p class="muted small">
						<code>{key}</code>
						{#if template.customised}
							· edited by {template.updatedBy ?? 'someone'} · version {template.version}
						{:else}
							· product default
						{/if}
						{#if categoryFor(key) && !categoryFor(key).enabled}
							· <span class="off">disabled</span>
						{/if}
					</p>
				</div>
				<div class="row">
					{#if categoryFor(key)}
						<button
							class="link"
							onclick={() => toggle(key, !categoryFor(key).enabled)}
							disabled={busy || key === 'OutOfScope'}
						>
							{categoryFor(key).enabled ? 'Disable' : 'Enable'}
						</button>
					{/if}
					{#if template.customised}
						<button class="link" onclick={() => revert(key)} disabled={busy}>
							Revert to default
						</button>
					{/if}
					<button class="link" onclick={() => edit(key)}>Edit</button>
				</div>
			</div>

			{#if editingKey === key}
				<label for="subject-{key}">Subject</label>
				<input id="subject-{key}" bind:value={draftSubject} />
				<label for="body-{key}">Message</label>
				<textarea id="body-{key}" rows="12" bind:value={draftBody}></textarea>
				<div class="row">
					<button onclick={save} disabled={busy}>Save</button>
					<button class="ghost" onclick={() => (editingKey = null)}>Cancel</button>
				</div>
			{:else}
				<p class="subject">{template.subject}</p>
				<pre>{template.body}</pre>
			{/if}
		</div>
	{/each}
{/if}

<style>
	.crumb {
		margin: 20px 0 0;
		font-size: 0.85rem;
	}
	h1 {
		font-size: 1.4rem;
		margin: 8px 0 4px;
	}
	h2 {
		font-size: 1.05rem;
		margin: 0;
	}
	h3 {
		font-size: 0.98rem;
		margin: 0 0 2px;
	}
	.lede {
		color: var(--muted);
		max-width: 66ch;
		margin: 0 0 20px;
	}
	.muted {
		color: var(--muted);
	}
	.small {
		font-size: 0.82rem;
	}
	.off {
		color: var(--warn);
	}
	.head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin: 28px 0 12px;
	}
	.box {
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 10px;
		padding: 16px;
		margin-bottom: 14px;
	}
	.slots ul {
		margin: 10px 0 0;
		padding-left: 18px;
		font-size: 0.86rem;
		columns: 2;
		column-gap: 28px;
	}
	.slots li {
		margin-bottom: 3px;
		break-inside: avoid;
	}
	.template-head {
		display: flex;
		justify-content: space-between;
		gap: 16px;
		align-items: flex-start;
		margin-bottom: 10px;
		flex-wrap: wrap;
	}
	.subject {
		font-weight: 600;
		margin: 0 0 8px;
		font-size: 0.9rem;
	}
	pre {
		white-space: pre-wrap;
		font: inherit;
		font-size: 0.88rem;
		line-height: 1.55;
		background: #f8f9fb;
		border: 1px solid var(--line);
		border-radius: 8px;
		padding: 14px 16px;
		margin: 0;
		overflow-x: auto;
	}
	label {
		display: block;
		font-size: 0.8rem;
		font-weight: 600;
		margin: 10px 0 4px;
	}
	input,
	textarea {
		width: 100%;
		padding: 8px 10px;
		border: 1px solid var(--line);
		border-radius: 6px;
		font: inherit;
		font-size: 0.9rem;
		box-sizing: border-box;
	}
	textarea {
		line-height: 1.55;
		resize: vertical;
	}
	.row {
		display: flex;
		gap: 12px;
		align-items: center;
		margin-top: 12px;
		flex-wrap: wrap;
	}
	.banner {
		border-radius: 8px;
		padding: 10px 14px;
		font-size: 0.88rem;
		margin: 0 0 12px;
	}
	.banner.err {
		background: #fdecea;
		color: var(--danger);
	}
	.banner.ok {
		background: #e7f6ec;
		color: #116329;
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
	button:disabled {
		opacity: 0.5;
		cursor: default;
	}
	button.link {
		background: none;
		color: var(--accent);
		padding: 0;
		font-weight: 400;
		font-size: 0.85rem;
	}
	button.ghost,
	.ghost {
		background: none;
		border: 1px solid var(--line);
		color: var(--text);
		font-size: 0.9rem;
	}
	code {
		background: #eef1f5;
		padding: 1px 5px;
		border-radius: 4px;
		font-size: 0.85em;
	}
</style>
