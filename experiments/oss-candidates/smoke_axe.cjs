/**
 * Isolated browser smoke: axe-core detects an intentionally missing image alt.
 * Runs only synthetic HTML in headless Chromium. No Imperial URL or credentials.
 */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const axe = require('axe-core');

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const runImageAlt = async (html) => {
      await page.setContent(html);
      await page.addScriptTag({ content: axe.source });
      return page.evaluate(() => window.axe.run(document, { runOnly: { type: 'rule', values: ['image-alt'] } }));
    };

    const good = await runImageAlt(
      '<!doctype html><html lang="en"><head><title>Good synthetic page</title></head><body><main><h1>Vehicle status</h1><img alt="Vehicle" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs="></main></body></html>'
    );
    assert.equal(good.violations.length, 0, 'valid synthetic image should pass');

    const bad = await runImageAlt(
      '<!doctype html><html lang="en"><head><title>Bad synthetic page</title></head><body><main><h1>Vehicle status</h1><img src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs="></main></body></html>'
    );
    assert.ok(bad.violations.some((v) => v.id === 'image-alt'), 'missing alt must be detected');
    console.log('axe-core + Playwright smoke PASS: known-good pass and known-bad detected');
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
