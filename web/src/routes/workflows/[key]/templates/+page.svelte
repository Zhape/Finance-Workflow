<script>
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { api } from '$lib/api.js';

	const key = $derived($page.params.key);

	// Order matters: this is the escalation ladder, and reading it top to bottom
	// should show the tone hardening.
	const ORDER = ['gentle', 'reminder', 'firm', 'final'];
	const LABELS = {
		gentle: { name: 'Gentle', when: '1–29 days overdue' },
		reminder: { name: 'Reminder', when: '30–59 days overdue' },
		firm: { name: 'Firm', when: '60–89 days overdue' },
		final: { name: 'Final notice', when: '90+ days overdue' }
	};

	// A worked example, so the preview shows real-looking output rather than
	// raw placeholders. Not fetched from Xero: an editor that needs a live API
	// call to show you your own wording is an editor people stop using.
	const SAMPLE = {
		first: 'Jane',
		customer: 'Jane Smith Ltd',
		count: 2,
		amount: '4,250.00',
		currency: 'GBP',
		days: 47,
		invoices: 'INV-1043, INV-1067',
		sender: 'Peter',
		plural: 's'
	};

	let data = $state(null);
	let drafts = $state({});
	let busy = $state('');
	let error = $state('');
	let notice = $state('');
	let selected = $state('gentle');

	onMount(load);

	async function load() {
		try {
			data = await api.templates(key);
			drafts = Object.fromEntries(
				Object.entries(data.templates).map(([k, v]) => [
					k,
					{ subject: v.subject, body: v.body }
				])
			);
			error = '';
		} catch (e) {
			error = e.message;
		}
	}

	function fill(text) {
		return (text ?? '').replace(/\{(\w+)\}/g, (match, token) =>
			token in SAMPLE ? String(SAMPLE[token]) : match
		);
	}

	const current = $derived(data?.templates?.[selected]);
	const draft = $derived(drafts[selected] ?? { subject: '', body: '' });
	const dirty = $derived(
		current && (draft.subject !== current.subject || draft.body !== current.body)
	);
	// Placeholders the editor does not know about will reach a customer's inbox
	// as literal braces, so they are worth pointing at before that happens.
	const unknown = $derived(
		[...new Set([...(draft.subject + ' ' + draft.body).matchAll(/\{(\w+)\}/g)].map((m) => m[1]))]
			.filter((t) => !(t in SAMPLE))
	);

	async function save() {
		busy = selected;
		error = '';
		notice = '';
		try {
			await api.saveTemplate(key, selected, draft.subject, draft.body);
			await load();
			notice = `${LABELS[selected].name} saved. New runs will use it.`;
		} catch (e) {
			error = e.message;
		}
		busy = '';
	}

	async function reset() {
		busy = selected;
		error = '';
		notice = '';
		try {
			await api.resetTemplate(key, selected);
			await load();
			notice = `${LABELS[selected].name} reverted to the standard wording.`;
		} catch (e) {
			error = e.message;
		}
		busy = '';
	}
</script>

<p class="crumb"><a href="/workflows/{key}">← Back to the workflow</a></p>
<h1>Chase-up messages</h1>
<p class="lede">
	Four messages, one per stage of lateness. Edit any of them; the ones you leave alone keep
	using the standard wording and improve when it does.
</p>

{#if notice}<div class="banner ok">{notice}</div>{/if}
{#if error}<div class="banner danger">{error}</div>{/if}

{#if data}
	<div class="tabs">
		{#each ORDER as variant}
			{@const t = data.templates[variant]}
			<button class="tab" class:active={selected === variant} onclick={() => (selected = variant)}>
				<span class="tabname">{LABELS[variant].name}</span>
				<span class="tabwhen">{LABELS[variant].when}</span>
				{#if t?.customised}<span class="dot" title="Customised"></span>{/if}
			</button>
		{/each}
	</div>

	<div class="editor">
		<div class="pane">
			<label for="subject">Subject</label>
			<input id="subject" type="text" bind:value={drafts[selected].subject} />

			<label for="body">Message</label>
			<textarea id="body" rows="16" bind:value={drafts[selected].body}></textarea>

			{#if unknown.length}
				<p class="warn">
					Unrecognised placeholder{unknown.length > 1 ? 's' : ''}:
					{#each unknown as u}<code>{'{' + u + '}'}</code>{' '}{/each}
					— these will appear literally in the email.
				</p>
			{/if}

			<div class="actions">
				<button onclick={save} disabled={!dirty || busy === selected}>
					{busy === selected ? 'Saving…' : 'Save'}
				</button>
				{#if current?.customised}
					<button class="ghost" onclick={reset} disabled={busy === selected}>
						Revert to standard
					</button>
				{/if}
				{#if current?.customised && current.updatedBy}
					<span class="by">Edited by {current.updatedBy}</span>
				{/if}
			</div>
		</div>

		<div class="pane">
			<div class="previewhead">Preview</div>
			<div class="preview">
				<div class="psubject">{fill(draft.subject) || '(no subject)'}</div>
				<div class="pbody">{fill(draft.body)}</div>
			</div>
			<p class="help">
				Filled with an example customer. Real runs use the customer's own figures.
			</p>

			<details class="tokens">
				<summary>Placeholders you can use</summary>
				<ul>
					{#each data.placeholders as p}
						<li><code>{p.token}</code> — {p.description}</li>
					{/each}
				</ul>
			</details>
		</div>
	</div>
{:else if !error}
	<p class="muted">Loading…</p>
{/if}

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
	.lede,
	.muted {
		color: var(--muted);
		margin: 0 0 20px;
		max-width: 62ch;
		line-height: 1.5;
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
	.tabs {
		display: flex;
		gap: 8px;
		flex-wrap: wrap;
		margin-bottom: 14px;
	}
	.tab {
		display: grid;
		gap: 2px;
		text-align: left;
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 8px;
		padding: 9px 14px;
		font: inherit;
		cursor: pointer;
		position: relative;
	}
	.tab.active {
		border-color: var(--accent);
		box-shadow: inset 0 0 0 1px var(--accent);
	}
	.tabname {
		font-weight: 600;
		font-size: 0.87rem;
	}
	.tabwhen {
		font-size: 0.74rem;
		color: var(--muted);
	}
	.dot {
		position: absolute;
		top: 8px;
		right: 8px;
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--accent);
	}
	.editor {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(330px, 1fr));
		gap: 16px;
		align-items: start;
	}
	.pane {
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 10px;
		padding: 16px;
	}
	label {
		display: block;
		font-size: 0.8rem;
		font-weight: 600;
		margin-bottom: 5px;
	}
	input,
	textarea {
		width: 100%;
		padding: 9px 10px;
		border: 1px solid var(--line);
		border-radius: 6px;
		font: inherit;
		font-size: 0.86rem;
		box-sizing: border-box;
		margin-bottom: 14px;
	}
	textarea {
		font-family: 'Segoe UI', system-ui, sans-serif;
		line-height: 1.5;
		resize: vertical;
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 10px;
		flex-wrap: wrap;
	}
	button {
		font: inherit;
		font-size: 0.85rem;
		font-weight: 600;
		padding: 8px 16px;
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
	.by {
		font-size: 0.78rem;
		color: var(--muted);
	}
	.previewhead {
		font-size: 0.8rem;
		font-weight: 600;
		margin-bottom: 8px;
	}
	.preview {
		border: 1px solid var(--line);
		border-radius: 8px;
		padding: 14px;
		background: #fbfcfd;
	}
	.psubject {
		font-weight: 600;
		font-size: 0.88rem;
		padding-bottom: 8px;
		margin-bottom: 10px;
		border-bottom: 1px solid var(--line);
	}
	.pbody {
		white-space: pre-wrap;
		font-size: 0.86rem;
		line-height: 1.55;
	}
	.help {
		font-size: 0.78rem;
		color: var(--muted);
		margin: 10px 0 0;
	}
	.warn {
		font-size: 0.8rem;
		color: var(--warn);
		background: var(--warn-bg);
		padding: 9px 11px;
		border-radius: 6px;
		margin: 0 0 12px;
	}
	.tokens {
		margin-top: 14px;
		font-size: 0.8rem;
		color: var(--muted);
	}
	.tokens summary {
		cursor: pointer;
	}
	.tokens ul {
		margin: 8px 0 0;
		padding-left: 18px;
		line-height: 1.7;
	}
	code {
		background: #eef1f5;
		padding: 1px 5px;
		border-radius: 4px;
		font-size: 0.85em;
	}
</style>
