import { api } from '$lib/api.js';

export const ssr = false;

export async function load({ params, fetch }) {
	try {
		return { run: await api.run(params.id, fetch) };
	} catch {
		return { run: null };
	}
}
