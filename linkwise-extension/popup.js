const BACKEND = 'https://linkedinagent.rev3.uk';

// ── Show a single state ────────────────────────────
function showState(n) {
  for (let i = 1; i <= 6; i++) {
    const el = document.getElementById('state' + i);
    if (el) el.classList.toggle('active', i === n);
  }
}

// ── Initialise popup ──────────────────────────────
chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => {
  const url = tab ? (tab.url || '') : '';
  if (url.includes('linkedin.com/in/')) {
    document.getElementById('tabUrl').textContent = url.replace('https://', '');
    showState(2);
  } else {
    showState(1);
  }
});

// ── STATE 1: Open LinkedIn ─────────────────────────
document.getElementById('openLinkedInBtn').addEventListener('click', () => {
  chrome.tabs.create({ url: 'https://www.linkedin.com/me' });
  window.close();
});

// ── STATE 2: Analyse ──────────────────────────────
document.getElementById('analyseBtn').addEventListener('click', async () => {
  showState(3);
  let handled = false;

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

    // Listen for message from content script
    const messagePromise = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Timed out reading your profile. Try reloading the LinkedIn page.')), 20000);
      chrome.runtime.onMessage.addListener(function handler(msg) {
        if (msg.type === 'PROFILE_EXTRACTED') {
          clearTimeout(timeout);
          chrome.runtime.onMessage.removeListener(handler);
          resolve(msg.profileText);
        }
      });
    });

    // Inject content script
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ['content.js'],
    });

    const profileText = await messagePromise;
    if (handled) return;
    handled = true;

    if (!profileText || profileText.length < 100) {
      throw new Error('Could not read enough profile data. Make sure you are on your full LinkedIn profile page.');
    }

    // STATE 4: sending
    showState(4);

    // Validate with backend
    const res = await fetch(`${BACKEND}/upload-text`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile_text: profileText }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Backend error (${res.status})`);
    }

    // Open REV3 tab, wait for load, set localStorage, navigate to extension URL
    const newTab = await chrome.tabs.create({
      url: BACKEND,
      active: true,
    });

    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Could not open REV3. Check your connection.')), 15000);
      function listener(tabId, changeInfo) {
        if (tabId === newTab.id && changeInfo.status === 'complete') {
          clearTimeout(timeout);
          chrome.tabs.onUpdated.removeListener(listener);
          resolve();
        }
      }
      chrome.tabs.onUpdated.addListener(listener);
    });

    // Set localStorage on the REV3 domain
    await chrome.scripting.executeScript({
      target: { tabId: newTab.id },
      func: (text) => { localStorage.setItem('linkwise_profile_text', text); },
      args: [profileText],
    });

    // Navigate to the extension entry point
    await chrome.tabs.update(newTab.id, { url: `${BACKEND}?source=extension` });

    // STATE 5: success
    showState(5);
    setTimeout(() => window.close(), 2000);

  } catch (err) {
    if (!handled) {
      showError(err.message || 'An unexpected error occurred.');
    }
  }
});

// ── STATE 6: Retry ────────────────────────────────
document.getElementById('retryBtn').addEventListener('click', () => {
  showState(2);
});

function showError(msg) {
  document.getElementById('errorMsg').textContent = msg;
  showState(6);
}
