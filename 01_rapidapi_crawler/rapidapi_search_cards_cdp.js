const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

const DEFAULT_START_URL = "https://rapidapi.com/search?sortBy=ByRelevance";
const DEFAULT_INPUT_JSON = "C:\\Users\\user\\Desktop\\host.json";
const DEFAULT_OUTPUT_JSON =
  "C:\\Users\\user\\Desktop\\glop_tta_k_release\\rapidapi_card_links.json";

function getArg(name, fallback) {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

function hasFlag(name) {
  return process.argv.includes(name);
}

function dedupeKeepOrder(values) {
  const seen = new Set();
  const result = [];
  for (const value of values) {
    const text = String(value || "").trim();
    if (text && !seen.has(text)) {
      seen.add(text);
      result.push(text);
    }
  }
  return result;
}

function loadKeywords(inputPath) {
  const data = JSON.parse(fs.readFileSync(inputPath, "utf8"));

  if (Array.isArray(data)) {
    return dedupeKeepOrder(
      data
        .map((item) => {
          if (typeof item === "string") return item;
          if (item && typeof item === "object") {
            return item.api_host || item["api-host"] || item.host;
          }
          return "";
        })
        .filter(Boolean),
    );
  }

  if (data && typeof data === "object") {
    for (const key of ["api_host", "api-host", "host", "keywords", "hosts"]) {
      const value = data[key];
      if (Array.isArray(value)) return dedupeKeepOrder(value);
      if (typeof value === "string") return [value];
    }
  }

  throw new Error(`Cannot read keywords from ${inputPath}`);
}

function findBrowserExecutable(customBrowserPath) {
  if (customBrowserPath && fs.existsSync(customBrowserPath)) return customBrowserPath;

  const candidates = [
    path.join(process.env.LOCALAPPDATA || "", "Microsoft\\Edge\\Application\\msedge.exe"),
    path.join(process.env.PROGRAMFILES || "", "Microsoft\\Edge\\Application\\msedge.exe"),
    path.join(
      process.env["PROGRAMFILES(X86)"] || "",
      "Microsoft\\Edge\\Application\\msedge.exe",
    ),
    path.join(process.env.LOCALAPPDATA || "", "Google\\Chrome\\Application\\chrome.exe"),
    path.join(process.env.PROGRAMFILES || "", "Google\\Chrome\\Application\\chrome.exe"),
    path.join(
      process.env["PROGRAMFILES(X86)"] || "",
      "Google\\Chrome\\Application\\chrome.exe",
    ),
  ];

  const browserPath = candidates.find((candidate) => candidate && fs.existsSync(candidate));
  if (!browserPath) {
    throw new Error("Cannot find Edge or Chrome. Use --browser C:\\path\\to\\msedge.exe");
  }
  return browserPath;
}

function startBrowser(browserPath, port) {
  const profileDir = path.join(__dirname, ".rapidapi-browser-profile");
  fs.mkdirSync(profileDir, { recursive: true });

  const args = [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profileDir}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-popup-blocking",
    "about:blank",
  ];

  return spawn(browserPath, args, {
    stdio: "ignore",
    detached: false,
  });
}

async function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status} ${url}`);
  }
  return response.json();
}

async function getPageWebSocketUrl(port) {
  const listUrl = `http://127.0.0.1:${port}/json`;
  const newUrl = `http://127.0.0.1:${port}/json/new?about:blank`;

  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const targets = await fetchJson(listUrl);
      const page = targets.find((target) => target.type === "page");
      if (page && page.webSocketDebuggerUrl) return page.webSocketDebuggerUrl;

      const created = await fetchJson(newUrl, { method: "PUT" });
      if (created.webSocketDebuggerUrl) return created.webSocketDebuggerUrl;
    } catch (_) {
      await sleep(250);
    }
  }

  throw new Error(`Cannot connect to browser debugging port ${port}`);
}

class CdpClient {
  constructor(wsUrl) {
    this.wsUrl = wsUrl;
    this.id = 0;
    this.pending = new Map();
  }

  async connect() {
    this.ws = new WebSocket(this.wsUrl);
    this.ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (!message.id || !this.pending.has(message.id)) return;

      const { resolve, reject } = this.pending.get(message.id);
      this.pending.delete(message.id);

      if (message.error) {
        reject(new Error(message.error.message || JSON.stringify(message.error)));
      } else {
        resolve(message.result);
      }
    });

    await new Promise((resolve, reject) => {
      this.ws.addEventListener("open", resolve, { once: true });
      this.ws.addEventListener("error", reject, { once: true });
    });
  }

  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
  }

  close() {
    if (this.ws) this.ws.close();
  }
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });

  if (result.exceptionDetails) {
    const message =
      result.exceptionDetails.exception?.description ||
      result.exceptionDetails.text ||
      "Runtime evaluation failed";
    throw new Error(message);
  }

  return result.result.value;
}

async function searchOneKeyword(cdp, keyword, waitMs) {
  await cdp.send("Page.navigate", { url: DEFAULT_START_URL });

  const expression = `
    (async () => {
      const keyword = ${JSON.stringify(keyword)};
      const waitMs = ${JSON.stringify(waitMs)};
      const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

      function findInput() {
        return document.querySelector('div[class="query-builder-input-wrapper absolute w-full self-stretch"] input')
          || document.querySelector('div.query-builder-input-wrapper input')
          || document.querySelector('input[type="search"]')
          || document.querySelector('input[placeholder*="Search"]')
          || document.querySelector('input[placeholder*="search"]');
      }

      function setInputValue(input, value) {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
        setter.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
      }

      let input = null;
      for (let attempt = 0; attempt < 300; attempt += 1) {
        input = findInput();
        if (input) break;
        await sleep(100);
      }
      if (!input) throw new Error("Search input not found");

      input.scrollIntoView({ block: "center", inline: "center" });
      input.focus();
      input.click();
      setInputValue(input, "");
      await sleep(100);
      setInputValue(input, keyword);

      input.dispatchEvent(new KeyboardEvent("keydown", {
        key: "Enter",
        code: "Enter",
        keyCode: 13,
        which: 13,
        bubbles: true,
        cancelable: true
      }));
      input.dispatchEvent(new KeyboardEvent("keyup", {
        key: "Enter",
        code: "Enter",
        keyCode: 13,
        which: 13,
        bubbles: true,
        cancelable: true
      }));

      await sleep(waitMs);

      const cardLinkRe = /^https:\\/\\/rapidapi\\.com\\/[^/?#]+\\/api\\/[^/?#]+/i;
      const seen = new Set();
      return Array.from(document.querySelectorAll("a[href]"))
        .map((anchor) => {
          let url = anchor.href || "";
          url = url.split("#")[0].split("?")[0];
          if (!cardLinkRe.test(url) || seen.has(url)) return null;
          seen.add(url);

          const title = (
            anchor.innerText ||
            anchor.getAttribute("aria-label") ||
            url.replace(/\\/$/, "").split("/").pop()
          ).trim();
          return { title, url };
        })
        .filter(Boolean);
    })()
  `;

  return evaluate(cdp, expression);
}

function saveJson(outputPath, data) {
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, JSON.stringify(data, null, 2), "utf8");
}

async function main() {
  const inputPath = getArg("--input", DEFAULT_INPUT_JSON);
  const outputPath = getArg("--output", DEFAULT_OUTPUT_JSON);
  const browserPath = findBrowserExecutable(getArg("--browser", ""));
  const port = Number(getArg("--port", "9223"));
  const waitMs = Number(getArg("--wait", "4000"));
  const limit = Number(getArg("--limit", "0"));
  const keepBrowser = hasFlag("--keep-browser");

  let keywords = loadKeywords(inputPath);
  if (limit > 0) keywords = keywords.slice(0, limit);

  console.log(`Browser: ${browserPath}`);
  console.log(`Input: ${inputPath}`);
  console.log(`Output: ${outputPath}`);

  const browserProcess = startBrowser(browserPath, port);
  const wsUrl = await getPageWebSocketUrl(port);
  const cdp = new CdpClient(wsUrl);
  const results = [];

  try {
    await cdp.connect();
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");

    for (let index = 0; index < keywords.length; index += 1) {
      const keyword = keywords[index];
      const item = { keyword, links: [], error: null };
      console.log(`[${index + 1}/${keywords.length}] Searching: ${keyword}`);

      try {
        item.links = await searchOneKeyword(cdp, keyword, waitMs);
        console.log(`  Found ${item.links.length} link(s)`);
      } catch (error) {
        item.error = error.message || String(error);
        console.log(`  Error: ${item.error}`);
      }

      results.push(item);
      saveJson(outputPath, results);
    }
  } finally {
    cdp.close();
    if (!keepBrowser) browserProcess.kill();
  }

  console.log(`Done. Saved to: ${outputPath}`);
}

main().catch((error) => {
  console.error(error.stack || error.message || error);
  process.exit(1);
});
