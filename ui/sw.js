/* Minimal install service worker.
 *
 * Chrome only offers the full "Install app" flow when the page is controlled by
 * a service worker that handles fetch. This worker deliberately caches nothing:
 * the field console must always fetch live status, tiles and feeds from the
 * server, and updates should never be masked by a stale cache.
 */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
