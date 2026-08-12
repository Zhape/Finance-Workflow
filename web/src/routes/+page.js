import { api } from '$lib/api.js';

export const ssr = false;

export async function load({ fetch }) {
	// A failure here is almost always "not signed in yet" or "no worker
	// configured". The layout explains both; throwing would replace that
	// explanation with an error page.
	try {
		const [{ workflows }, { runs }] = await Promise.all([
			api.workflows(fetch),
			api.runs(fetch)
		]);
		return { workflows, runs };
	} catch {
		return { workflows: [], runs: [] };
	}
}
