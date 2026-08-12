import { api } from '$lib/api.js';

export const ssr = false;

export async function load({ fetch }) {
	// A failure here is almost always "not signed in yet" or "no worker
	// configured". The layout explains both; throwing would replace that
	// explanation with an error page.
	try {
		const [{ workflows, tools }, { runs }] = await Promise.all([
			api.workflows(fetch),
			api.runs(fetch)
		]);
		// `tools` are granted the same way but have their own screen rather
		// than a launch form. Defaulted so a stale worker still renders.
		return { workflows, tools: tools ?? [], runs };
	} catch {
		return { workflows: [], tools: [], runs: [] };
	}
}
