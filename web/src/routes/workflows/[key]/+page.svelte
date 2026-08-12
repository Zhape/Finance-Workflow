<script>
	import { goto } from '$app/navigation';
	import { api } from '$lib/api.js';

	let { data } = $props();
	const wf = data.workflow ?? { name: '', description: '', params: [], key: '' };

	// The form is generated from the workflow's ParamSpec list — adding a
	// workflow never means writing a form.
	let values = $state(
		Object.fromEntries(wf.params.map((p) => [p.name, p.default ?? (p.type === 'bool' ? false : '')]))
	);
	let running = $state(false);
	let fieldErrors = $state({});
	let error = $state('');

	async function submit(event) {
		event.preventDefault();
		running = true;
		error = '';
		fieldErrors = {};
		try {
			const run = await api.start(wf.key, values);
			await goto(`/runs/${run.id}`);
		} catch (e) {
			try {
				fieldErrors = JSON.parse(e.message).fieldErrors ?? {};
			} catch {
				error = e.message;
			}
			running = false;
		}
	}
</script>

<p class="crumb"><a href="/">← All workflows</a></p>
<h1>{wf.name}</h1>
<p class="lede">{wf.description}</p>

<form onsubmit={submit}>
	{#each wf.params as p (p.name)}
		<div class="field">
			<label for={p.name}>
				{p.label}
				{#if !p.required}<span class="opt">optional</span>{/if}
			</label>

			{#if p.type === 'choice'}
				<select id={p.name} bind:value={values[p.name]}>
					{#each p.options as option}
						<option value={option}>{option}</option>
					{/each}
				</select>
			{:else if p.type === 'date'}
				<input id={p.name} type="date" bind:value={values[p.name]} />
			{:else if p.type === 'bool'}
				<label class="check">
					<input id={p.name} type="checkbox" bind:checked={values[p.name]} />
					<span>{p.help}</span>
				</label>
			{:else}
				<input id={p.name} type="text" bind:value={values[p.name]} />
			{/if}

			{#if p.help && p.type !== 'bool'}<p class="help">{p.help}</p>{/if}
			{#if fieldErrors[p.name]}<p class="err">{fieldErrors[p.name]}</p>{/if}
		</div>
	{/each}

	{#if error}<p class="err banner">{error}</p>{/if}

	<button type="submit" disabled={running}>
		{running ? 'Pulling from Xero…' : 'Run'}
	</button>
	{#if wf.requiresApproval}
		<p class="note">
			This produces a list for review. Nothing is sent or paid until someone approves it.
		</p>
	{/if}
</form>

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
	.lede {
		color: var(--muted);
		margin: 0 0 24px;
	}
	form {
		max-width: 460px;
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 10px;
		padding: 22px;
	}
	.field {
		margin-bottom: 18px;
	}
	label {
		display: block;
		font-size: 0.87rem;
		font-weight: 600;
		margin-bottom: 6px;
	}
	.opt {
		font-weight: 400;
		color: var(--muted);
		font-size: 0.78rem;
		margin-left: 6px;
	}
	input[type='text'],
	input[type='date'],
	select {
		width: 100%;
		padding: 8px 10px;
		border: 1px solid var(--line);
		border-radius: 6px;
		font: inherit;
		background: #fff;
		box-sizing: border-box;
	}
	.check {
		display: flex;
		gap: 8px;
		align-items: flex-start;
		font-weight: 400;
		font-size: 0.85rem;
		color: var(--muted);
	}
	.check input {
		margin-top: 2px;
	}
	.help {
		font-size: 0.78rem;
		color: var(--muted);
		margin: 6px 0 0;
	}
	.err {
		color: var(--danger);
		font-size: 0.82rem;
		margin: 6px 0 0;
	}
	.err.banner {
		background: #fdecea;
		padding: 10px 12px;
		border-radius: 6px;
		margin-bottom: 14px;
	}
	button {
		font: inherit;
		font-weight: 600;
		padding: 9px 20px;
		border: none;
		border-radius: 6px;
		background: var(--accent);
		color: #fff;
		cursor: pointer;
	}
	button:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.note {
		font-size: 0.78rem;
		color: var(--muted);
		margin: 12px 0 0;
	}
</style>
