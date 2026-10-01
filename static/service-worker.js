// Copyright (C) 2026 Mathew Levett
// SPDX-License-Identifier: AGPL-3.0-or-later

const CACHE = 'raynet-logger-shell-v92';
const fromScope = path => new URL(path, self.registration.scope).toString();
const SHELL = ['./', 'static/styles.css?v=82', 'static/app.js?v=86', 'static/icon.svg', 'static/default-brand-logo.png', 'manifest.webmanifest'].map(fromScope);
self.addEventListener('install', event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL))));
self.addEventListener('activate', event => event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))));
self.addEventListener('fetch', event => {
  const apiPath = new URL('api/', self.registration.scope).pathname;
  if (event.request.method !== 'GET' || new URL(event.request.url).pathname.startsWith(apiPath)) return;
  event.respondWith(fetch(event.request).then(response => {
    const copy = response.clone(); caches.open(CACHE).then(cache => cache.put(event.request, copy)); return response;
  }).catch(() => caches.match(event.request)));
});
