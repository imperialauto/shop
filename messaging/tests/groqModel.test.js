const test = require("node:test");
const assert = require("node:assert/strict");
const { DEFAULT_MODEL, RETIRED, resolveGroqModel } = require("../src/lib/groqModel");

test("default model uses supported Groq free-tier candidate", () => {
  assert.equal(DEFAULT_MODEL, "openai/gpt-oss-120b");
  assert.equal(resolveGroqModel(null), DEFAULT_MODEL);
  assert.equal(resolveGroqModel(""), DEFAULT_MODEL);
});

test("retired Groq model IDs cannot silently remain configured", () => {
  assert.equal(resolveGroqModel("llama-3.3-70b-versatile"), "openai/gpt-oss-120b");
  assert.equal(resolveGroqModel("llama-3.1-8b-instant"), "openai/gpt-oss-20b");
  assert.equal(resolveGroqModel("qwen/qwen3.6-27b"), "qwen/qwen3.8-27b");
  for (const [retired, replacement] of Object.entries(RETIRED)) {
    assert.notEqual(retired, replacement, retired);
    assert.equal(resolveGroqModel(retired), replacement);
  }
});

test("current owner-selected Groq models are preserved exactly", () => {
  assert.equal(resolveGroqModel("openai/gpt-oss-20b"), "openai/gpt-oss-20b");
  assert.equal(resolveGroqModel("qwen/qwen3.8-27b"), "qwen/qwen3.8-27b");
  assert.equal(resolveGroqModel("openai/gpt-oss-120b"), "openai/gpt-oss-120b");
  assert.equal(resolveGroqModel("  openai/gpt-oss-120b  "), "openai/gpt-oss-120b");
});

test("both owner SMS and customer draft code consume resolved model", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const lib = path.join(__dirname, "../src/lib");
  for (const filename of ["ownerAssistant.js","generateDraftReply.js"]) {
    const source = fs.readFileSync(path.join(lib, filename), "utf8");
    assert.match(source,/const MODEL = resolveGroqModel\(\);/);
    assert.doesNotMatch(source, /GROQ_DRAFT_MODEL \|\| "llama-3\.3-70b-versatile"/);
  }
});
