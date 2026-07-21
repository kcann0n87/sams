"""Anti-automation-detection patches applied to each page.

Bot-detection systems (like the PerimeterX "press and hold" Sam's Club uses)
look for tells that a browser is automated — chiefly `navigator.webdriver`.
These init scripts run before any page script and paper over the most common
tells so the automated session looks like an ordinary Chrome session.

This is not a magic bypass: it reduces detection, it doesn't guarantee it.
The strongest lever is pairing this with a real Chrome profile that has already
passed a challenge (a "warmed" profile) and your normal home IP.
"""

STEALTH_INIT_JS = r"""
// navigator.webdriver -> undefined (the #1 automation tell)
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Make sure a chrome runtime object exists like in real Chrome
window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || {};

// Plausible language + plugin arrays
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });

// Permissions.query shouldn't reveal an odd notifications state
const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (_origQuery) {
  window.navigator.permissions.query = (params) =>
    params && params.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : _origQuery(params);
}
"""
