const fs = require("fs");
const path = require("path");

const DEFAULT_INPUT_JSON = path.join(__dirname, "rapidapi_card_links.json");
const DEFAULT_OUTPUT_JSON = path.join(__dirname, "rapidapi_api_metadata.json");
const USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
const HTTP_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]);

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

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function saveJson(filePath, data) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, JSON.stringify(data, null, 2), "utf8");
}

function cleanText(value) {
  return String(value || "")
    .replace(/\r\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
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

function normalizeRapidApiUrl(rawUrl) {
  if (!rawUrl) return "";
  const url = new URL(rawUrl);
  const parts = url.pathname.split("/").filter(Boolean);
  const apiIndex = parts.indexOf("api");
  if (apiIndex > 0 && parts[apiIndex + 1]) {
    return "https://rapidapi.com/" + parts[apiIndex - 1] + "/api/" + parts[apiIndex + 1] + "/playground";
  }
  const playgroundIndex = parts.indexOf("playground");
  if (playgroundIndex >= 0) url.pathname = "/" + parts.slice(0, playgroundIndex + 1).join("/");
  url.search = "";
  url.hash = "";
  return url.toString();
}

function inferApiSlugFromUrl(rawUrl) {
  try {
    const parts = new URL(rawUrl).pathname.split("/").filter(Boolean);
    const apiIndex = parts.indexOf("api");
    if (apiIndex >= 0 && parts[apiIndex + 1]) return parts[apiIndex + 1];
  } catch (_) {}
  return "";
}

function flattenInput(data) {
  const entries = [];
  if (Array.isArray(data)) {
    for (const item of data) {
      if (typeof item === "string") {
        entries.push({ url: item });
      } else if (item && typeof item === "object" && Array.isArray(item.links)) {
        for (const link of item.links) {
          if (link && link.url) entries.push({ keyword: item.keyword || "", title: link.title || "", url: link.url });
        }
      } else if (item && typeof item === "object" && (item.url || item.api_link)) {
        entries.push({
          keyword: item.keyword || item.category || "",
          title: item.title || item.api_name || item.name || "",
          url: item.url || item.api_link,
          card: item,
        });
      }
    }
  }

  const seen = new Set();
  const result = [];
  for (const entry of entries) {
    const url = normalizeRapidApiUrl(entry.url);
    if (!url || seen.has(url)) continue;
    seen.add(url);
    result.push(Object.assign({}, entry, { url }));
  }
  return result;
}

async function fetchText(url, retries, timeoutMs) {
  let lastError = null;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        redirect: "follow",
        signal: controller.signal,
        headers: {
          accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
          "accept-language": "en-US,en;q=0.9",
          "user-agent": USER_AGENT,
        },
      });
      if (!response.ok) throw new Error("HTTP " + response.status + " " + response.statusText);
      return await response.text();
    } catch (error) {
      lastError = error;
      if (attempt < retries) await sleep(800 * (attempt + 1));
    } finally {
      clearTimeout(timer);
    }
  }
  throw lastError;
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

function extractBalancedJsonAt(text, start) {
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
    else if (char === "{") depth += 1;
    else if (char === "}") {
      depth -= 1;
      if (depth === 0) return text.slice(start, index + 1);
    }
  }
  return null;
}

function extractDehydratedQueries(flightText) {
  const queries = [];
  let offset = 0;
  while ((offset = flightText.indexOf('{"dehydratedAt"', offset)) >= 0) {
    const jsonText = extractBalancedJsonAt(flightText, offset);
    if (!jsonText) break;
    try { queries.push(JSON.parse(jsonText)); } catch (_) {}
    offset += jsonText.length;
  }
  return queries;
}

function getQueryData(queries, queryName) {
  const item = queries.find((query) => query.queryKey && query.queryKey[0] === queryName);
  return item ? item.state && item.state.data : null;
}

function parseRapidApiPage(html) {
  const flight = extractNextFlight(html);
  const queries = extractDehydratedQueries(flight);
  const api = getQueryData(queries, "getApiBySlug");
  const version = getQueryData(queries, "getApiVersion");
  if (!api && !version) throw new Error("Cannot find RapidAPI metadata in page data");
  return { api, version };
}

function parseMaybeJson(value) {
  if (value == null) return null;
  if (typeof value !== "string") return value;
  const text = value.trim();
  if (!text) return null;
  if ((text.startsWith("{") && text.endsWith("}")) || (text.startsWith("[") && text.endsWith("]"))) {
    try { return JSON.parse(text); } catch (_) { return value; }
  }
  return value;
}

function toSnakeCase(value) {
  return String(value || "")
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^a-zA-Z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/_+/g, "_")
    .toLowerCase();
}

function coerceParameterList(value) {
  const parsed = parseMaybeJson(value && value.parameters !== undefined ? value.parameters : value);
  if (Array.isArray(parsed)) return parsed;
  if (!parsed || typeof parsed !== "object") return [];

  const result = [];
  for (const key of ["query", "queries", "queryParams", "path", "pathParams", "headers"]) {
    if (Array.isArray(parsed[key])) {
      for (const item of parsed[key]) result.push(Object.assign({}, item, { in: item.in || key }));
    }
  }
  if (result.length > 0) return result;
  return Object.values(parsed).every((item) => item && typeof item === "object") ? Object.values(parsed) : [];
}

function parameterName(parameter) {
  return parameter.name || parameter.key || parameter.id || parameter.param || parameter.title || "";
}

function parameterLocation(parameter) {
  return String(parameter.in || parameter.paramType || parameter.parameterType || parameter.location || parameter.type || "query").toLowerCase();
}

function parameterValue(parameter) {
  for (const key of ["value", "example", "examples", "default", "defaultValue", "testValue", "sample"]) {
    if (parameter[key] !== undefined && parameter[key] !== null && parameter[key] !== "") {
      const value = parseMaybeJson(parameter[key]);
      if (value && typeof value === "object" && !Array.isArray(value)) {
        if (value.value !== undefined) return value.value;
        if (value.example !== undefined) return value.example;
      }
      return value;
    }
  }
  return "";
}

function splitRoute(route) {
  const parts = String(route || "").split("?");
  const pathPart = parts[0] || "/";
  const queryParams = {};
  if (parts[1]) {
    for (const pair of new URLSearchParams(parts.slice(1).join("?")).entries()) queryParams[pair[0]] = pair[1];
  }
  return { pathPart, queryParams };
}

function collectParams(endpoint) {
  const params = {};
  const pathParams = {};
  const headerParams = {};
  Object.assign(params, splitRoute(endpoint.route).queryParams);

  for (const parameter of coerceParameterList(endpoint.params)) {
    if (!parameter || typeof parameter !== "object") continue;
    const name = parameterName(parameter);
    if (!name) continue;
    const location = parameterLocation(parameter);
    const value = parameterValue(parameter);
    if (location.includes("header")) headerParams[name] = value;
    else if (location.includes("path")) pathParams[name] = value;
    else if (!location.includes("body")) params[name] = value;
  }

  return {
    params: Object.keys(params).length > 0 ? params : null,
    pathParams,
    headerParams,
  };
}

function fillPathParams(routePath, pathParams) {
  return String(routePath || "/").replace(/\{([^}]+)\}|:([A-Za-z0-9_]+)/g, function(match, a, b) {
    const name = a || b;
    const value = pathParams[name];
    return value === undefined || value === "" ? match : encodeURIComponent(String(value));
  });
}

function getRapidApiHost(version, fallbackSlug) {
  const dnsItems = Array.isArray(version && version.publicdns) ? version.publicdns : [];
  const current = dnsItems.find((item) => item && item.current && item.address);
  const first = dnsItems.find((item) => item && item.address);
  const host = (current || first || {}).address || "";
  if (host) return host;
  const slug = String(fallbackSlug || "").trim();
  return slug ? slug + ".p.rapidapi.com" : "";
}

function getBaseUrl(version, rapidApiHost) {
  if (rapidApiHost) return "https://" + rapidApiHost;
  const targetUrls = version && version.targetGroup && Array.isArray(version.targetGroup.targetUrls) && version.targetGroup.targetUrls;
  const targetUrl = targetUrls && targetUrls.find((item) => item && item.url);
  return targetUrl ? targetUrl.url.replace(/\/+$/, "") : "";
}

function makeEndpointUrl(baseUrl, route, pathParams) {
  const routePath = fillPathParams(splitRoute(route).pathPart, pathParams);
  if (/^https?:\/\//i.test(routePath)) return routePath;
  if (!baseUrl) return routePath;
  return baseUrl.replace(/\/+$/, "") + "/" + routePath.replace(/^\/+/, "");
}

function parsePayloadExamples(examples) {
  const parsed = parseMaybeJson(examples);
  if (parsed == null) return null;
  if (Array.isArray(parsed)) {
    for (const item of parsed) {
      const value = parsePayloadExamples(item);
      if (value != null) return value;
    }
    return null;
  }
  if (typeof parsed === "object") {
    for (const key of ["body", "value", "example", "payload", "data"]) {
      if (parsed[key] !== undefined) return parseMaybeJson(parsed[key]);
    }
    return parsed;
  }
  return parsed;
}

function parseFormPayload(text) {
  if (typeof text !== "string" || !text.includes("=")) return null;
  const params = new URLSearchParams(text);
  const result = {};
  for (const pair of params.entries()) result[pair[0]] = pair[1];
  return Object.keys(result).length > 0 ? result : null;
}

function chooseRequestPayload(endpoint) {
  const payloads = Array.isArray(endpoint.requestPayloads) ? endpoint.requestPayloads : [];
  return payloads.find((payload) => cleanText(payload && payload.body)) ||
    payloads.find((payload) => payload && payload.examples) ||
    payloads[0] ||
    null;
}

function inferContentType(requestPayload) {
  const text = String(requestPayload.format || requestPayload.type || requestPayload.contentType || requestPayload.headers || "").toLowerCase();
  if (text.includes("form")) return "application/x-www-form-urlencoded";
  if (text.includes("xml")) return "application/xml";
  if (text.includes("json")) return "application/json";
  return requestPayload ? "application/json" : "";
}

function parseRequestPayload(endpoint) {
  const method = String(endpoint.method || "").toUpperCase();
  const requestPayload = chooseRequestPayload(endpoint);
  if (!requestPayload) return { payload: null, contentType: "" };
  const contentType = inferContentType(requestPayload);
  let payload = parseMaybeJson(requestPayload.body);
  if (payload == null) payload = parsePayloadExamples(requestPayload.examples);
  if (payload == null && typeof requestPayload.body === "string") payload = parseFormPayload(requestPayload.body);
  if (payload == null && method && method !== "GET") payload = {};
  return { payload, contentType };
}

function buildDescription(endpoint) {
  const method = String(endpoint.method || "").toUpperCase();
  const name = cleanText(endpoint.name || endpoint.route || "endpoint");
  const description = cleanText(endpoint.description || "");
  const lines = [method, name].filter(Boolean);
  if (description && description.toLowerCase() !== name.toLowerCase()) lines.push(description);
  return lines.join("\n");
}

function metadataFromEndpoint(endpoint, version, apiSlug) {
  const method = String(endpoint.method || "GET").toUpperCase();
  const rapidApiHost = getRapidApiHost(version, apiSlug);
  const baseUrl = getBaseUrl(version, rapidApiHost);
  const collected = collectParams(endpoint);
  const parsedPayload = parseRequestPayload(endpoint);
  const headers = {};
  if (rapidApiHost) headers["x-rapidapi-host"] = rapidApiHost;
  for (const key of Object.keys(collected.headerParams)) headers[key] = collected.headerParams[key];
  if (parsedPayload.payload != null && parsedPayload.contentType && method !== "GET") headers["Content-Type"] = parsedPayload.contentType;

  return {
    endpoint_id: endpoint.id || "",
    endpoint_name: cleanText(endpoint.name || ""),
    route: endpoint.route || "",
    method,
    rapidapi_host: rapidApiHost,
    base_url: baseUrl,
    url: makeEndpointUrl(baseUrl, endpoint.route, collected.pathParams),
    headers,
    params: collected.params,
    path_params: Object.keys(collected.pathParams).length > 0 ? collected.pathParams : null,
    header_params: Object.keys(collected.headerParams).length > 0 ? collected.headerParams : null,
    payload: method === "GET" && parsedPayload.payload == null ? null : parsedPayload.payload,
    content_type: parsedPayload.contentType || "",
    description: buildDescription(endpoint),
    endpoint_description: cleanText(endpoint.description || ""),
    is_graphql: Boolean(endpoint.isGraphQL),
    group: endpoint.group || null,
    index: endpoint.index ?? null,
    parameters_raw: parseMaybeJson(endpoint.params),
    request_payloads: Array.isArray(endpoint.requestPayloads) ? endpoint.requestPayloads.map(parseMaybeJson) : [],
    response_payloads: Array.isArray(endpoint.responsePayloads) ? endpoint.responsePayloads.map(parseMaybeJson) : [],
    external_docs: endpoint.externalDocs || null,
    security: endpoint.security || null,
  };
}

function buildMetadataDocument(entry, parsedPage, includeSource) {
  const api = parsedPage.api || {};
  const version = parsedPage.version || {};
  const endpoints = Array.isArray(version.endpoints) ? version.endpoints : [];
  const apiSlug = api.slugifiedName || inferApiSlugFromUrl(entry.url);
  const apiName = apiSlug || api.name || entry.title || "";
  const rapidApiHost = getRapidApiHost(version, apiSlug);
  const owner = api.owner || {};
  const card = entry.card || {};
  const document = {
    api_name: apiName,
    api_title: api.title || api.name || entry.title || card.title || "",
    api_description: cleanText(api.description || api.shortDescription || card.description || ""),
    api_long_description: cleanText(api.longDescription || ""),
    api_id: api.id || card.api_id || "",
    api_slug: apiSlug,
    category: api.category || card.category || "",
    category_id: api.categoryId || "",
    api_category: api.apiCategory || card.api_category || null,
    pricing: api.pricing || card.pricing || "",
    visibility: api.visibility || card.visibility || "",
    updated_at: api.updatedAt || card.updated_at || "",
    created_at: api.createdAt || "",
    thumbnail: api.thumbnail || card.thumbnail || "",
    owner: {
      id: owner.id ?? (card.owner && card.owner.id) ?? null,
      name: owner.name || (card.owner && card.owner.name) || "",
      username: owner.username || owner.slugifiedName || (card.owner && card.owner.username) || "",
      slugified_name: owner.slugifiedName || owner.username || (card.owner && card.owner.slugified_name) || "",
      type: owner.type || (card.owner && card.owner.type) || "",
      thumbnail: owner.thumbnail || "",
    },
    score: api.score || card.score || null,
    quality: api.quality || null,
    rating: api.rating || null,
    subscriptions_count: api.subscriptionsCount ?? null,
    website_url: api.websiteUrl || "",
    terms_of_service: api.termsOfService || null,
    documentation: api.documentation || null,
    version_id: version.id || "",
    version_name: version.name || "",
    version_status: version.versionStatus || "",
    rapidapi_host: rapidApiHost,
    base_url: getBaseUrl(version, rapidApiHost),
    function: dedupeKeepOrder(endpoints.map((endpoint) => toSnakeCase(endpoint.name || endpoint.route)).filter(Boolean)),
    endpoints_metadata: endpoints
      .filter((endpoint) => endpoint && HTTP_METHODS.has(String(endpoint.method || "").toUpperCase()))
      .map((endpoint) => metadataFromEndpoint(endpoint, version, apiSlug)),
    endpoint_count: endpoints.filter((endpoint) => endpoint && HTTP_METHODS.has(String(endpoint.method || "").toUpperCase())).length,
  };
  if (includeSource) {
    document.source_url = entry.url;
    document.keyword = entry.keyword || null;
    document.title = entry.title || api.title || null;
    document.source_card = card && Object.keys(card).length > 0 ? card : null;
  }
  return document;
}

function buildErrorDocument(entry, error, includeSource) {
  const document = {
    api_name: inferApiSlugFromUrl(entry.url) || entry.title || entry.url,
    function: [],
    endpoints_metadata: [],
    error: error && error.message ? error.message : String(error),
  };
  if (includeSource) {
    document.source_url = entry.url;
    document.keyword = entry.keyword || null;
    document.title = entry.title || null;
  }
  return document;
}

async function crawlOne(entry, options) {
  const entryWithUrl = Object.assign({}, entry, { url: normalizeRapidApiUrl(entry.url || entry.api_link) });
  const html = await fetchText(entryWithUrl.url, options.retries, options.timeoutMs);
  return buildMetadataDocument(entryWithUrl, parseRapidApiPage(html), options.includeSource);
}

async function runPool(items, concurrency, worker) {
  let nextIndex = 0;
  const workers = Array.from({ length: Math.min(concurrency, items.length) }, async function() {
    while (nextIndex < items.length) {
      const index = nextIndex;
      nextIndex += 1;
      await worker(items[index], index);
    }
  });
  await Promise.all(workers);
}

async function main() {
  const inputPath = path.resolve(getArg("--input", DEFAULT_INPUT_JSON));
  const outputPath = path.resolve(getArg("--output", DEFAULT_OUTPUT_JSON));
  const limit = toInt(getArg("--limit", "0"), 0);
  const concurrency = Math.max(1, toInt(getArg("--concurrency", "2"), 2));
  const delayMs = toInt(getArg("--delay", "300"), 300);
  const retries = toInt(getArg("--retries", "2"), 2);
  const timeoutMs = Math.max(1000, toInt(getArg("--timeout", "45000"), 45000));
  const includeSource = hasFlag("--include-source");
  const force = hasFlag("--force");

  let entries = flattenInput(readJson(inputPath));
  if (limit > 0) entries = entries.slice(0, limit);

  const existing = !force && fs.existsSync(outputPath) ? readJson(outputPath) : [];
  const done = new Set(Array.isArray(existing) ? existing.map((item) => item && item.api_name).filter(Boolean) : []);
  const results = Array.isArray(existing) && !force ? existing.slice() : [];
  const tasks = entries.filter((entry) => !done.has(inferApiSlugFromUrl(entry.url)));

  console.log("Input links: " + entries.length);
  console.log("Already done: " + (entries.length - tasks.length));
  console.log("To crawl: " + tasks.length);
  console.log("Output: " + outputPath);

  await runPool(tasks, concurrency, async function(entry, index) {
    if (delayMs > 0) await sleep(delayMs * (index % concurrency));
    console.log("[" + (index + 1) + "/" + tasks.length + "] " + entry.url);

    let document;
    try {
      document = await crawlOne(entry, { retries, timeoutMs, includeSource });
      console.log("  " + document.api_name + ": " + document.endpoints_metadata.length + " endpoint(s)");
    } catch (error) {
      document = buildErrorDocument(entry, error, includeSource);
      console.log("  Error: " + document.error);
    }

    results.push(document);
    saveJson(outputPath, results);
  });

  saveJson(outputPath, results);
  console.log("Done. Saved to: " + outputPath);
}

if (require.main === module) {
  main().catch(function(error) {
    console.error(error.stack || error.message || error);
    process.exit(1);
  });
}

module.exports = {
  crawlOne,
  flattenInput,
  inferApiSlugFromUrl,
  normalizeRapidApiUrl,
  readJson,
  saveJson,
  runPool,
};
