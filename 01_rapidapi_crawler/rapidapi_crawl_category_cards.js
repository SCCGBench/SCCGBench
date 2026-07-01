const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const BASE_URL = "https://rapidapi.com";
const CATEGORIES_URL = `${BASE_URL}/categories`;
const GRAPHQL_URL = `${BASE_URL}/gateway/graphql`;
const CSRF_URL = `${BASE_URL}/gateway/csrf`;
const DEFAULT_OUTPUT_DIR = path.join(__dirname, "rapidapi_category_cards");
const USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";

const SEARCH_APIS_QUERY = `
query searchApis(
  $searchApiWhereInput: SearchApiWhereInput!
  $paginationInput: PaginationInput
  $searchApiOrderByInput: SearchApiOrderByInput
) {
  products: searchApis(
    where: $searchApiWhereInput
    pagination: $paginationInput
    orderBy: $searchApiOrderByInput
  ) {
    nodes {
      id
      thumbnail
      name
      description
      slugifiedName
      pricing
      updatedAt
      categoryName
      isSavedApi
      title
      visibility
      category: categoryName
      apiCategory {
        name
        color
      }
      score {
        popularityScore
        avgLatency
        avgServiceLevel
        avgSuccessRate
      }
      version {
        tags {
          id
          status
          tagdefinition
          type
          value
        }
      }
      user: User {
        id
        username
        slugifiedName: username
        name
        type
        parents {
          id
          name
          slugifiedName
          type
          thumbnail
        }
      }
    }
    facets {
      category {
        key
        count
      }
    }
    pageInfo {
      endCursor
      hasNextPage
      hasPreviousPage
      startCursor
    }
    total
    queryID
    replicaIndex
  }
}
`;

function getArg(name, fallback) {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

function hasFlag(name) {
  return process.argv.includes(name);
}

function toInt(value, fallback) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function saveJson(filePath, data) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, JSON.stringify(data, null, 2), "utf8");
}

function readJson(filePath, fallback) {
  if (!fs.existsSync(filePath)) return fallback;
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function safeFilePart(value) {
  return String(value || "unknown")
    .replace(/[<>:"/\\|?*\x00-\x1F]+/g, "_")
    .replace(/\s+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 120);
}

function extractNextFlight(html) {
  const chunks = [];
  const re = /self\.__next_f\.push\((.*?)\)<\/script>/gs;
  let match;
  while ((match = re.exec(html))) {
    try {
      const value = JSON.parse(match[1]);
      if (typeof value[1] === "string") chunks.push(value[1]);
    } catch (_) {}
  }
  return chunks.join("\n");
}

function extractBalanced(text, start, openChar, closeChar) {
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = start; index < text.length; index += 1) {
    const char = text[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') inString = false;
      continue;
    }

    if (char === '"') inString = true;
    else if (char === openChar) depth += 1;
    else if (char === closeChar) {
      depth -= 1;
      if (depth === 0) return text.slice(start, index + 1);
    }
  }
  return null;
}

function extractCategories(html) {
  const flight = extractNextFlight(html);
  const marker = '"categories":';
  const markerIndex = flight.indexOf(marker);
  if (markerIndex < 0) {
    throw new Error("Cannot find categories in RapidAPI page data");
  }

  const arrayStart = flight.indexOf("[", markerIndex + marker.length);
  const arrayText = extractBalanced(flight, arrayStart, "[", "]");
  if (!arrayText) throw new Error("Cannot parse categories array");

  return JSON.parse(arrayText).sort((a, b) => (a.weight || 0) - (b.weight || 0));
}

function cardUrl(card) {
  const owner = card && card.user && (card.user.slugifiedName || card.user.username);
  const slug = card && card.slugifiedName;
  if (!owner || !slug) return "";
  return `${BASE_URL}/${encodeURIComponent(owner)}/api/${encodeURIComponent(slug)}/playground`;
}

function ownerUrl(card) {
  const owner = card && card.user && (card.user.slugifiedName || card.user.username);
  return owner ? `${BASE_URL}/user/${encodeURIComponent(owner)}` : "";
}

function normalizeCard(card, category) {
  return {
    api_name: card.name || card.title || card.slugifiedName || "",
    title: card.title || card.name || "",
    api_link: cardUrl(card),
    description: card.description || "",
    category: category.name,
    category_slug: category.slugifiedName,
    api_id: card.id || "",
    slugified_name: card.slugifiedName || "",
    pricing: card.pricing || "",
    updated_at: card.updatedAt || "",
    visibility: card.visibility || "",
    thumbnail: card.thumbnail || "",
    owner: card.user
      ? {
          id: card.user.id ?? null,
          name: card.user.name || "",
          username: card.user.username || "",
          slugified_name: card.user.slugifiedName || card.user.username || "",
          type: card.user.type || "",
          link: ownerUrl(card),
          parents: card.user.parents || null,
        }
      : null,
    score: card.score || null,
    api_category: card.apiCategory || null,
    tags: card.version && Array.isArray(card.version.tags) ? card.version.tags : [],
  };
}

class CookieJar {
  constructor() {
    this.cookies = new Map();
  }

  add(setCookies) {
    for (const cookie of setCookies || []) {
      const pair = String(cookie).split(";")[0];
      const index = pair.indexOf("=");
      if (index > 0) this.cookies.set(pair.slice(0, index), pair.slice(index + 1));
    }
  }

  header() {
    return Array.from(this.cookies.entries())
      .map(([key, value]) => `${key}=${value}`)
      .join("; ");
  }
}

class RapidApiSession {
  constructor() {
    this.jar = new CookieJar();
    this.csrfToken = "";
  }

  async fetchWithCookies(url, options = {}) {
    const headers = {
      "user-agent": USER_AGENT,
      accept: "*/*",
      ...(options.headers || {}),
    };
    const cookie = this.jar.header();
    if (cookie) headers.cookie = cookie;

    const response = await fetch(url, { ...options, headers });
    if (typeof response.headers.getSetCookie === "function") {
      this.jar.add(response.headers.getSetCookie());
    }
    return response;
  }

  async initialize() {
    await this.fetchText(CATEGORIES_URL, { accept: "text/html" });
    await this.refreshCsrf();
  }

  async refreshCsrf() {
    const response = await this.fetchWithCookies(CSRF_URL, {
      headers: {
        accept: "application/json",
        referer: CATEGORIES_URL,
      },
    });
    if (!response.ok) throw new Error(`CSRF request failed: HTTP ${response.status}`);
    const data = await response.json();
    this.csrfToken = data.csrfToken || "";
    if (!this.csrfToken) throw new Error("CSRF token is empty");
  }

  async fetchText(url, headers = {}) {
    const response = await this.fetchWithCookies(url, { headers });
    if (!response.ok) throw new Error(`Request failed: HTTP ${response.status} ${url}`);
    return response.text();
  }

  async gql(operationName, query, variables, referer, retries) {
    let lastError = null;
    for (let attempt = 0; attempt <= retries; attempt += 1) {
      try {
        const response = await this.fetchWithCookies(GRAPHQL_URL, {
          method: "POST",
          headers: {
            accept: "application/json",
            "content-type": "application/json",
            origin: BASE_URL,
            referer,
            "rapid-client": "hub-service",
            "x-correlation-id": crypto.randomUUID(),
            "csrf-token": this.csrfToken,
          },
          body: JSON.stringify({ operationName, query, variables }),
        });
        const text = await response.text();
        if (!response.ok) throw new Error(`GraphQL HTTP ${response.status}: ${text.slice(0, 300)}`);

        const data = JSON.parse(text);
        if (data.errors && data.errors.length) {
          const message = data.errors.map((item) => item.message).join(" | ");
          if (/419|csrf/i.test(message)) await this.refreshCsrf();
          throw new Error(message);
        }
        return data.data;
      } catch (error) {
        lastError = error;
        if (attempt < retries) await sleep(1000 * (attempt + 1));
      }
    }
    throw lastError;
  }
}

async function fetchCategories(session, outputDir) {
  const html = await session.fetchText(CATEGORIES_URL, { accept: "text/html" });
  const categories = extractCategories(html);
  saveJson(path.join(outputDir, "categories.json"), categories);
  return categories;
}

function categoryFile(outputDir, category) {
  const weight = String(category.weight || 0).padStart(2, "0");
  return path.join(outputDir, "by_category", `${weight}_${safeFilePart(category.slugifiedName || category.name)}.json`);
}

function categoryReferer(category, sort) {
  return `${BASE_URL}/search/${encodeURIComponent(category.name)}?sortBy=${encodeURIComponent(sort)}`;
}

function loadExistingCategory(filePath) {
  const existing = readJson(filePath, null);
  if (!existing || !Array.isArray(existing.cards)) return null;
  return existing;
}

async function crawlCategory(session, category, options) {
  const filePath = categoryFile(options.outputDir, category);
  const referer = categoryReferer(category, options.sort);
  const existing = options.force ? null : loadExistingCategory(filePath);

  if (existing && existing.completed) {
    existing.target_limit = existing.target_limit || options.apiLimitPerCategory;
    existing.source_has_more = Boolean(existing.total && existing.cards.length < existing.total);
    existing.truncated = false;
    if (existing.source_has_more && options.apiLimitPerCategory > 0 && existing.cards.length >= options.apiLimitPerCategory) {
      existing.stopped_by_limit = true;
    }
    saveJson(filePath, existing);
    console.log(`  Skip completed: ${existing.cards.length}/${existing.total ?? "?"}`);
    return existing;
  }
  if (
    existing &&
    existing.total &&
    existing.cards.length < existing.total &&
    !existing.next_cursor &&
    !options.force
  ) {
    existing.completed = true;
    existing.target_limit = options.apiLimitPerCategory;
    existing.source_has_more = true;
    existing.truncated = false;
    existing.stopped_by_limit = false;
    saveJson(filePath, existing);
    console.log(`  Target already reached: ${existing.cards.length}/${existing.total}`);
    return existing;
  }

  const cards = existing ? existing.cards.slice() : [];
  const seen = new Set(cards.map((card) => card.api_id || card.api_link).filter(Boolean));
  let cursor = existing ? existing.next_cursor || "" : "";
  let hasNextPage = true;
  let total = existing ? existing.total || null : null;
  let page = existing ? existing.pages_crawled || 0 : 0;
  let stoppedByLimit = false;

  while (hasNextPage) {
    if (options.apiLimitPerCategory > 0 && cards.length >= options.apiLimitPerCategory) {
      stoppedByLimit = true;
      hasNextPage = false;
      break;
    }

    const remaining =
      options.apiLimitPerCategory > 0 ? Math.max(1, options.apiLimitPerCategory - cards.length) : options.first;
    const first = Math.min(options.first, remaining);
    const variables = {
      paginationInput: { first, after: cursor || "" },
      searchApiWhereInput: {
        term: "",
        categoryNames: [category.name],
      },
      searchApiOrderByInput: {
        sortingFields: [{ fieldName: options.sort, by: "ASC" }],
      },
    };

    const data = await session.gql("searchApis", SEARCH_APIS_QUERY, variables, referer, options.retries);
    const products = data.products || {};
    const pageInfo = products.pageInfo || {};
    const nodes = Array.isArray(products.nodes) ? products.nodes : [];
    total = products.total ?? total;

    for (const card of nodes) {
      const normalized = normalizeCard(card, category);
      const key = normalized.api_id || normalized.api_link;
      if (!key || seen.has(key)) continue;
      seen.add(key);
      cards.push(normalized);
    }

    cursor = pageInfo.endCursor || "";
    hasNextPage = Boolean(pageInfo.hasNextPage && cursor);
    page += 1;

    const document = {
      category,
      total,
      crawled_count: cards.length,
      pages_crawled: page,
      next_cursor: hasNextPage ? cursor : null,
      target_limit: options.apiLimitPerCategory,
      completed: stoppedByLimit || (!hasNextPage && !(total && cards.length < total)),
      source_has_more: Boolean(total && cards.length < total),
      truncated: false,
      stopped_by_limit: stoppedByLimit,
      last_crawled_at: new Date().toISOString(),
      sort: options.sort,
      cards,
    };
    saveJson(filePath, document);

    console.log(`  Page ${page}: +${nodes.length}, saved ${cards.length}/${total ?? "?"}`);
    if (options.delayMs > 0) await sleep(options.delayMs);
  }

  const finalDocument = loadExistingCategory(filePath);
  if (finalDocument && stoppedByLimit) {
    finalDocument.completed = true;
    finalDocument.target_limit = options.apiLimitPerCategory;
    finalDocument.source_has_more = Boolean(finalDocument.total && finalDocument.cards.length < finalDocument.total);
    finalDocument.truncated = false;
    finalDocument.stopped_by_limit = true;
    finalDocument.last_crawled_at = new Date().toISOString();
    saveJson(filePath, finalDocument);
    return finalDocument;
  }
  if (finalDocument && !finalDocument.completed && !hasNextPage && !stoppedByLimit) {
    finalDocument.completed = !(finalDocument.total && finalDocument.cards.length < finalDocument.total);
    finalDocument.target_limit = options.apiLimitPerCategory;
    finalDocument.source_has_more = Boolean(finalDocument.total && finalDocument.cards.length < finalDocument.total);
    finalDocument.truncated = false;
    finalDocument.next_cursor = null;
    finalDocument.stopped_by_limit = false;
    finalDocument.last_crawled_at = new Date().toISOString();
    saveJson(filePath, finalDocument);
    return finalDocument;
  }
  return loadExistingCategory(filePath);
}

function writeSummary(outputDir, categories, results) {
  const summary = categories.map((category) => {
    const result = results.find((item) => item && item.category && item.category.id === category.id);
    return {
      id: category.id,
      name: category.name,
      slugified_name: category.slugifiedName,
      weight: category.weight,
      total: result ? result.total : null,
      crawled_count: result ? result.crawled_count : 0,
      pages_crawled: result ? result.pages_crawled : 0,
      completed: result ? Boolean(result.completed) : false,
      target_limit: result ? result.target_limit || null : null,
      source_has_more: result ? Boolean(result.source_has_more) : false,
      stopped_by_limit: result ? Boolean(result.stopped_by_limit) : false,
      file: path.relative(outputDir, categoryFile(outputDir, category)).replace(/\\/g, "/"),
    };
  });
  saveJson(path.join(outputDir, "summary.json"), {
    generated_at: new Date().toISOString(),
    category_count: categories.length,
    total_cards: summary.reduce((sum, item) => sum + (item.crawled_count || 0), 0),
    completed_categories: summary.filter((item) => item.completed).length,
    categories_with_more_source_results: summary.filter((item) => item.source_has_more).length,
    categories: summary,
  });
}

async function main() {
  const outputDir = path.resolve(getArg("--output-dir", DEFAULT_OUTPUT_DIR));
  const categoryLimit = toInt(getArg("--category-limit", "0"), 0);
  const apiLimitPerCategory = toInt(getArg("--api-limit-per-category", "1000"), 1000);
  const first = Math.min(100, Math.max(1, toInt(getArg("--first", "100"), 100)));
  const delayMs = toInt(getArg("--delay", "500"), 500);
  const retries = toInt(getArg("--retries", "3"), 3);
  const concurrency = Math.max(1, toInt(getArg("--concurrency", "1"), 1));
  const sort = getArg("--sort", "ByRelevance");
  const categoryName = getArg("--category", "");
  const force = hasFlag("--force");

  const session = new RapidApiSession();
  await session.initialize();

  let categories = await fetchCategories(session, outputDir);
  if (categoryName) {
    const wanted = categoryName.toLowerCase();
    categories = categories.filter(
      (category) =>
        String(category.name || "").toLowerCase() === wanted ||
        String(category.slugifiedName || "").toLowerCase() === wanted,
    );
    if (categories.length === 0) throw new Error(`Category not found: ${categoryName}`);
  }
  if (categoryLimit > 0) categories = categories.slice(0, categoryLimit);

  console.log(`Output: ${outputDir}`);
  console.log(`Categories to crawl: ${categories.length}`);
  console.log(`Page size: ${first}`);
  console.log(`Concurrency: ${concurrency}`);

  const results = new Array(categories.length).fill(null);
  let nextIndex = 0;
  async function worker(workerIndex) {
    const workerSession = workerIndex === 0 ? session : new RapidApiSession();
    if (workerIndex !== 0) await workerSession.initialize();

    while (nextIndex < categories.length) {
      const index = nextIndex;
      nextIndex += 1;
      const category = categories[index];
      console.log(`[${index + 1}/${categories.length}] ${category.name}`);
      const result = await crawlCategory(workerSession, category, {
        outputDir,
        first,
        delayMs,
        retries,
        sort,
        force,
        apiLimitPerCategory,
      });
      results[index] = result;
      writeSummary(outputDir, categories, results.filter(Boolean));
    }
  }

  const workerCount = Math.min(concurrency, categories.length);
  await Promise.all(Array.from({ length: workerCount }, (_, index) => worker(index)));

  writeSummary(outputDir, categories, results.filter(Boolean));
  console.log(`Done. Saved to: ${outputDir}`);
}

main().catch((error) => {
  console.error(error.stack || error.message || error);
  process.exit(1);
});
