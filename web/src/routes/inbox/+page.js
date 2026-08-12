import { api } from '$lib/api.js';

export const ssr = false;

export async function load({ fetch }) {
	// A failure here is almost always "not signed in" or "this org does not
	// have the inbox". The page explains both; throwing would replace that
	// explanation with an error screen.
	try {
		const [status, { emails }] = await Promise.all([
			api.inboxStatus(fetch),
			api.inboxEmails(fetch)
		]);
		return { status, emails, available: true };
	} catch (e) {
		return { status: null, emails: [], available: false, reason: e.message };
	}
}
