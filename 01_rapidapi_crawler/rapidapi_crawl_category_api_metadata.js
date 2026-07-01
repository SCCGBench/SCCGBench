const fs = require("fs");
const path = require("path");

const {
  crawlOne,
  inferApiSlugFromUrl,
  normalizeRapidApiUrl,
  readJson,
  saveJson,
} = require("./rapidapi_crawl_api_metadata");

const DEFAULT_INPUT_DIR = path.join(__dirname, "rapidapi_category_cards", "by_category");
const DEFAULT_OUTPUT_DIR = path.join(__dirname, "rapidapi_category_api_metadata");

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

function readJsonIfExists(filePath, fallback) {
  if (!fs.existsSync(filePath)) return fallback;
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function listCategoryFiles(inputDir) {
  return fs
    .readdirSync(inputDir)
    .filter((file) => file.toLowerCase().endsWith(".json"))
    .sort()
    .map((file) => path.join(inputDir, file));
}

function outputFileFor(outputDir, inputFile) {
  return path.join(outputDir, "by_category", path.basename(inputFile));
}

function apiKeyFromCard(card) {
  return normalizeRapidApiUrl(card.api_link || card.url || "") || card.api_id || card.api_name || card.title || "";
}

function apiKeyFromMetadata(item) {
  return (
    normalizeRapidApiUrl(item.source_url || (item.source_card && (item.source_card.api_link || item.source_card.url)) || "") ||
    item.api_id ||
    item.api_slug ||
    item.api_name ||
    item.title ||
    ""
  );
}

function toEntry(card, category) {
  return {
    url: normalizeRapidApiUrl(card.api_link || card.url || ""),
    title: card.title || card.api_name || "",
    keyword: category.name || card.category || "",
    category: category.name || card.category || "",
    card,
  };
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

function buildCategoryDocument(source, apis, options) {
  const endpointCount = apis.reduce(
    (sum, item) => sum + (Array.isArray(item.endpoints_metadata) ? item.endpoints_metadata.length : 0),
    0,
  );
  return {
    category: source.category,
    source_total: source.total ?? null,
    source_card_count: Array.isArray(source.cards) ? source.cards.length : 0,
    crawled_count: apis.length,
    error_count: apis.filter((item) => item.error).length,
    endpoint_count: endpointCount,
    completed: apis.length >= (Array.isArray(source.cards) ? source.cards.length : 0),
    last_crawled_at: new Date().toISOString(),
    source_card_sort: source.sort || "",
    source_has_more: Boolean(source.source_has_more),
    target_limit: source.target_limit || options.apiLimitPerCategory || null,
    apis,
  };
}

async function crawlCategoryFile(inputFile, outputDir, options) {
  const source = readJson(inputFile);
  const outputFile = outputFileFor(outputDir, inputFile);
  const existing = options.force ? null : readJsonIfExists(outputFile, null);
  const category = source.category || {};
  const cards = Array.isArray(source.cards) ? source.cards : [];
  const limitedCards =
    options.apiLimitPerCategory > 0 ? cards.slice(0, options.apiLimitPerCategory) : cards.slice();

  if (existing && options.retryErrors && Array.isArray(existing.apis)) {
    const before = existing.apis.length;
    existing.apis = existing.apis.filter((item) => !item.error);
    if (existing.apis.length < before) {
      existing.completed = false;
      console.log(`  Retry errors: ${before - existing.apis.length}`);
    }
  }

  if (existing && existing.completed && Array.isArray(existing.apis) && existing.apis.length >= limitedCards.length) {
    console.log(`  Skip completed: ${existing.apis.length}/${limitedCards.length}`);
    return existing;
  }

  const apis = existing && Array.isArray(existing.apis) && !options.force ? existing.apis.slice() : [];
  const done = new Set(apis.map(apiKeyFromMetadata).filter(Boolean));
  const tasks = limitedCards.filter((card) => {
    const key = apiKeyFromCard(card);
    return key && !done.has(key);
  });

  console.log(`  APIs: ${limitedCards.length}, already done: ${limitedCards.length - tasks.length}, to crawl: ${tasks.length}`);
  saveJson(outputFile, buildCategoryDocument(source, apis, options));

  await runPool(tasks, options.apiConcurrency, async function(card, index) {
    const entry = toEntry(card, category);
    let document;
    try {
      if (options.delayMs > 0) await sleep(options.delayMs * (index % options.apiConcurrency));
      document = await crawlOne(entry, {
        retries: options.retries,
        timeoutMs: options.timeoutMs,
        includeSource: options.includeSource,
      });
      console.log(`    [${index + 1}/${tasks.length}] ${document.api_name}: ${document.endpoint_count || 0} endpoint(s)`);
    } catch (error) {
      document = {
        api_name: card.api_name || card.title || apiKeyFromCard(card),
        api_slug: inferApiSlugFromUrl(card.api_link || ""),
        api_title: card.title || card.api_name || "",
        category: category.name || card.category || "",
        source_url: card.api_link || "",
        source_card: options.includeSource ? card : null,
        function: [],
        endpoints_metadata: [],
        endpoint_count: 0,
        error: error && error.message ? error.message : String(error),
      };
      console.log(`    [${index + 1}/${tasks.length}] Error: ${document.api_name} - ${document.error}`);
    }

    apis.push(document);
    saveJson(outputFile, buildCategoryDocument(source, apis, options));
  });

  const finalDocument = buildCategoryDocument(source, apis, options);
  saveJson(outputFile, finalDocument);
  return finalDocument;
}

function writeSummary(outputDir, documents) {
  const categories = documents.map((document) => ({
    name: document.category && document.category.name,
    slugified_name: document.category && document.category.slugifiedName,
    source_total: document.source_total,
    source_card_count: document.source_card_count,
    crawled_count: document.crawled_count,
    error_count: document.error_count,
    endpoint_count: document.endpoint_count,
    completed: document.completed,
    file: path
      .relative(outputDir, outputFileFor(outputDir, `${String(document.category && document.category.weight).padStart(2, "0")}_${document.category && document.category.slugifiedName}.json`))
      .replace(/\\/g, "/"),
  }));
  saveJson(path.join(outputDir, "summary.json"), {
    generated_at: new Date().toISOString(),
    category_count: categories.length,
    completed_categories: categories.filter((item) => item.completed).length,
    total_apis: categories.reduce((sum, item) => sum + item.crawled_count, 0),
    total_errors: categories.reduce((sum, item) => sum + item.error_count, 0),
    total_endpoints: categories.reduce((sum, item) => sum + item.endpoint_count, 0),
    categories,
  });
}

async function main() {
  const inputDir = path.resolve(getArg("--input-dir", DEFAULT_INPUT_DIR));
  const outputDir = path.resolve(getArg("--output-dir", DEFAULT_OUTPUT_DIR));
  const categoryLimit = toInt(getArg("--category-limit", "0"), 0);
  const apiLimitPerCategory = toInt(getArg("--api-limit-per-category", "1000"), 1000);
  const categoryConcurrency = Math.max(1, toInt(getArg("--category-concurrency", "1"), 1));
  const apiConcurrency = Math.max(1, toInt(getArg("--api-concurrency", "4"), 4));
  const delayMs = toInt(getArg("--delay", "200"), 200);
  const retries = toInt(getArg("--retries", "2"), 2);
  const timeoutMs = Math.max(1000, toInt(getArg("--timeout", "45000"), 45000));
  const includeSource = !hasFlag("--no-source");
  const force = hasFlag("--force");
  const retryErrors = hasFlag("--retry-errors");

  let files = listCategoryFiles(inputDir);
  if (categoryLimit > 0) files = files.slice(0, categoryLimit);

  console.log(`Input: ${inputDir}`);
  console.log(`Output: ${outputDir}`);
  console.log(`Categories: ${files.length}`);
  console.log(`API limit per category: ${apiLimitPerCategory}`);
  console.log(`Category concurrency: ${categoryConcurrency}`);
  console.log(`API concurrency/category: ${apiConcurrency}`);
  console.log(`Retry errors: ${retryErrors ? "yes" : "no"}`);

  const documents = new Array(files.length).fill(null);
  let nextIndex = 0;
  async function categoryWorker() {
    while (nextIndex < files.length) {
      const index = nextIndex;
      nextIndex += 1;
      const file = files[index];
      const source = readJson(file);
      const categoryName = source.category && source.category.name ? source.category.name : path.basename(file);
      console.log(`[${index + 1}/${files.length}] ${categoryName}`);
      const document = await crawlCategoryFile(file, outputDir, {
        apiLimitPerCategory,
        apiConcurrency,
        delayMs,
        retries,
        timeoutMs,
        includeSource,
        force,
        retryErrors,
      });
      documents[index] = document;
      writeSummary(outputDir, documents.filter(Boolean));
    }
  }

  await Promise.all(Array.from({ length: Math.min(categoryConcurrency, files.length) }, categoryWorker));
  writeSummary(outputDir, documents.filter(Boolean));
  console.log(`Done. Saved to: ${outputDir}`);
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error.stack || error.message || error);
    process.exit(1);
  });
}
