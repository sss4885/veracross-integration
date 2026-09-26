#!/usr/bin/env node

/**
 * generate_screenshots.js
 *
 * Generates high-resolution screenshots of the Veracross Lovelace card using
 * generic dummy data from docs/card-demo.html.
 *
 * Zero external dependencies (uses Node.js 20+ built-in fetch and WebSocket,
 * driving headless Chrome/Chromium via Chrome DevTools Protocol).
 *
 * Usage:
 *   node scripts/generate_screenshots.js
 */

const { spawn, execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const repoRoot = path.resolve(__dirname, '..');
const docsDir = path.join(repoRoot, 'docs');
const outDir = path.join(docsDir, 'screenshots');
const demoHtmlPath = path.join(docsDir, 'card-demo.html');

if (!fs.existsSync(outDir)) {
  fs.mkdirSync(outDir, { recursive: true });
}

function findChrome() {
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const candidates = [
    'google-chrome',
    'google-chrome-stable',
    'chromium',
    'chromium-browser',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium-browser',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ];
  for (const cmd of candidates) {
    try {
      const p = execSync(`which "${cmd}" 2>/dev/null`).toString().trim();
      if (p) return p;
    } catch {
      // ignore
    }
  }
  throw new Error('Chrome/Chromium binary not found. Set CHROME_PATH or install Chrome.');
}

async function main() {
  const chromeBin = findChrome();
  console.log(`Using browser: ${chromeBin}`);

  const port = 9222 + Math.floor(Math.random() * 500);
  const chrome = spawn(chromeBin, [
    '--headless=new',
    `--remote-debugging-port=${port}`,
    '--disable-gpu',
    '--no-sandbox',
    '--hide-scrollbars',
    '--window-size=1200,1600',
    '--force-device-scale-factor=2',
    'about:blank'
  ]);

  let killed = false;
  const cleanup = () => {
    if (!killed) {
      killed = true;
      chrome.kill();
    }
  };
  process.on('exit', cleanup);
  process.on('SIGINT', () => { cleanup(); process.exit(1); });
  process.on('SIGTERM', () => { cleanup(); process.exit(1); });

  // Wait for remote debugging endpoint
  let connected = false;
  for (let i = 0; i < 30; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (res.ok) {
        connected = true;
        break;
      }
    } catch {
      await new Promise(r => setTimeout(r, 100));
    }
  }
  if (!connected) {
    throw new Error('Failed to connect to headless Chrome remote debugging port.');
  }

  try {
    const newTabRes = await fetch(`http://127.0.0.1:${port}/json/new?file://${demoHtmlPath}`, { method: 'PUT' });
    const tab = await newTabRes.json();

    const ws = new WebSocket(tab.webSocketDebuggerUrl);
    await new Promise(resolve => ws.onopen = resolve);

    let id = 1;
    const send = (method, params = {}) => new Promise((resolve, reject) => {
      const curId = id++;
      const handler = (m) => {
        const d = JSON.parse(m.data);
        if (d.id === curId) {
          ws.removeEventListener('message', handler);
          if (d.error) reject(d.error);
          else resolve(d.result);
        }
      };
      ws.addEventListener('message', handler);
      ws.send(JSON.stringify({ id: curId, method, params }));
    });

    await send('Runtime.enable');
    await send('Page.enable');
    await send('DOM.enable');

    await new Promise(r => setTimeout(r, 1000));

    // Style page container for clean presentation
    await send('Runtime.evaluate', {
      expression: `
        (() => {
          document.querySelector(".controls").style.display = "none";
          document.body.style.padding = "24px";
          document.body.style.display = "flex";
          document.body.style.justifyContent = "center";
          document.body.style.alignItems = "flex-start";
          document.body.style.backgroundColor = "#0b131e";
          const card = document.querySelector("veracross-card");
          card.style.width = "640px";
          card.style.maxWidth = "100%";
        })()
      `
    });

    async function evaluateCode(code, awaitPromise = false) {
      const res = await send('Runtime.evaluate', {
        expression: code,
        awaitPromise,
        returnByValue: true
      });
      if (res.exceptionDetails) {
        throw new Error('Evaluation error: ' + JSON.stringify(res.exceptionDetails));
      }
      return res.result?.value;
    }

    async function captureElement(filename) {
      await new Promise(r => setTimeout(r, 200));
      const rect = await evaluateCode(`
        (() => {
          const card = document.querySelector("veracross-card");
          const r = card.getBoundingClientRect();
          return { x: r.x, y: r.y, width: r.width, height: r.height };
        })()
      `);

      const pad = 12;
      const shot = await send('Page.captureScreenshot', {
        format: 'png',
        clip: {
          x: Math.max(0, rect.x - pad),
          y: Math.max(0, rect.y - pad),
          width: rect.width + (pad * 2),
          height: rect.height + (pad * 2),
          scale: 1
        }
      });

      const dest = path.join(outDir, filename);
      fs.writeFileSync(dest, Buffer.from(shot.data, 'base64'));
      console.log(`Saved: docs/screenshots/${filename} (${Math.round(rect.width)}x${Math.round(rect.height)})`);
    }

    // 1. PIN Keypad (locked state)
    console.log('1. Capturing PIN keypad (locked)...');
    await evaluateCode(`(() => { setActive("none"); })()`);
    await captureElement('01_keypad_locked.png');

    // 2. Student Overview (grades revealed)
    console.log('2. Capturing student overview (grades shown)...');
    await evaluateCode(`(() => { setActive("S1"); })()`);
    await evaluateCode(`
      new Promise(resolve => {
        const poll = () => {
          const c = document.querySelector("veracross-card");
          if (c._data && c._studentId === "S1") resolve();
          else setTimeout(poll, 30);
        };
        poll();
      })
    `, true);
    await evaluateCode(`
      (() => {
        const c = document.querySelector("veracross-card");
        if (c._gradesHidden) {
          c.shadowRoot.querySelector("[data-action=eye]")?.click();
        }
        const ovTab = Array.from(c.shadowRoot.querySelectorAll("[data-action=select]")).find(el => el.dataset.index === "overview");
        if (ovTab) ovTab.click();
      })()
    `);
    await captureElement('02_student_overview.png');

    // 3. Assignment Detail (expanded notes and teacher feedback)
    console.log('3. Capturing assignment detail (expanded)...');
    await evaluateCode(`
      (() => {
        const c = document.querySelector("veracross-card");
        const firstRow = c.shadowRoot.querySelector("[data-action=row]");
        if (firstRow && firstRow.getAttribute("aria-expanded") !== "true") {
          firstRow.click();
        }
      })()
    `);
    await captureElement('03_assignment_feedback_expanded.png');

    // 4. Class View (Math with weighted categories)
    console.log('4. Capturing class categories view (Math)...');
    await evaluateCode(`
      (() => {
        const c = document.querySelector("veracross-card");
        const mathChip = Array.from(c.shadowRoot.querySelectorAll("[data-action=select]")).find(el => el.dataset.index === "1");
        if (mathChip) mathChip.click();
      })()
    `);
    await captureElement('04_class_categories_math.png');

    // 5. Parent Switcher (Multiple students view)
    console.log('5. Capturing parent multi-student view...');
    await evaluateCode(`(() => { setActive("all"); })()`);
    await evaluateCode(`
      new Promise(resolve => {
        const poll = () => {
          const c = document.querySelector("veracross-card");
          if (c._data && c._active === "all") resolve();
          else setTimeout(poll, 30);
        };
        poll();
      })
    `, true);
    await evaluateCode(`
      (() => {
        const c = document.querySelector("veracross-card");
        if (c._gradesHidden) {
          c.shadowRoot.querySelector("[data-action=eye]")?.click();
        }
        const ovTab = Array.from(c.shadowRoot.querySelectorAll("[data-action=select]")).find(el => el.dataset.index === "overview");
        if (ovTab) ovTab.click();
      })()
    `);
    await captureElement('05_parent_multi_student.png');

    // 6. Privacy Mode (eye toggle masking overall grades)
    console.log('6. Capturing privacy mode (grades masked)...');
    await evaluateCode(`(() => { setActive("S1"); })()`);
    await evaluateCode(`
      new Promise(resolve => {
        const poll = () => {
          const c = document.querySelector("veracross-card");
          if (c._data && c._studentId === "S1") resolve();
          else setTimeout(poll, 30);
        };
        poll();
      })
    `, true);
    await evaluateCode(`
      (() => {
        const c = document.querySelector("veracross-card");
        if (!c._gradesHidden) {
          c.shadowRoot.querySelector("[data-action=eye]")?.click();
        }
        const ovTab = Array.from(c.shadowRoot.querySelectorAll("[data-action=select]")).find(el => el.dataset.index === "overview");
        if (ovTab) ovTab.click();
      })()
    `);
    await captureElement('06_privacy_masked.png');

    console.log('\nAll screenshots captured successfully in docs/screenshots/!');

  } finally {
    cleanup();
  }
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
